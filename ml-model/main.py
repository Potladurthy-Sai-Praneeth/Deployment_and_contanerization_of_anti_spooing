from fastapi import FastAPI, UploadFile, File, Form, HTTPException, status
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Optional, List
import numpy as np
import base64
import json
from io import BytesIO
from PIL import Image
from generate_embedding import generate_face_embedding
from authenticate import FaceAuthenticator
import uvicorn
import os


class AddUserResponse(BaseModel):
    message: str
    user_name: str
    is_saved: bool
    embedding: Optional[List[float]] = Field(default=None, description="128-dimensional face embedding vector as list")

class AuthenticateResponse(BaseModel):
    is_authenticated: bool
    user_name: Optional[str] = None


app = FastAPI(
    title="Anti Spoofing API",
    description="Anti-spoofing face recognition system with user management",
    version="1.0.0",
    docs_url="/docs",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],  # Changed from ["POST"] to ["*"] for consistency
    allow_headers=["*"],
)

# Global variables
size = os.getenv("IMAGE_SIZE", "252")  # Default to 252 if not set
provider = os.getenv('EXECUTION_PROVIDER','CPUExecutionProvider')  # Change to 'CUDAExecutionProvider' if GPU is available

authenticator = FaceAuthenticator(execution_provider=provider, image_size=(int(size), int(size)))

def decode_base64_image(image_data: str) -> np.ndarray:
    """Decode base64 image to numpy array using PIL"""
    try:
        if "," in image_data:
            image_data = image_data.split(",")[1]
        
        image_bytes = base64.b64decode(image_data)
        
        pil_image = Image.open(BytesIO(image_bytes))
        
        if pil_image.mode != 'RGB':
            pil_image = pil_image.convert('RGB')
        
        frame = np.array(pil_image)
        
        return frame
    except Exception as e:
        print(f'Error:{str(e)}')
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid image data: {str(e)}"
        )

def validate_image_file(image: UploadFile) -> None:
    """Validate uploaded image file"""
    if not image.content_type or not image.content_type.startswith('image/'):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File must be an image"
        )

def validate_user_name(user_name: str) -> str:
    """Validate and sanitize user name"""
    if not user_name or not user_name.strip():

        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User name cannot be empty"
        )
    return user_name.strip().lower()
    


# API Endpoints

@app.get("/health")
async def health_check():
    """Health check endpoint."""
    try:
        return {
            "status": "healthy",
            "message": "ML service is running",
            "model_loaded": authenticator is not None,
            "image_size": (int(size), int(size)),
            "provider": provider
        }
    except Exception as e:
        return {"status": "unhealthy", "message": str(e)}


@app.post("/getEmbedding", response_model=AddUserResponse)
async def add_user(
    image: UploadFile = File(..., description="User's face image"),
    user_name: str = Form(..., description="User's name")
) -> AddUserResponse:
    """Add a new user by uploading their image and generating face embeddings."""
    try:
        # Validate inputs
        validate_image_file(image)
        user_name = validate_user_name(user_name)
        
        # Process image
        contents = await image.read()
        
        # Validate image content
        if len(contents) == 0:
            return AddUserResponse(
                is_saved=False,
                user_name=user_name,
                message="Empty image file",
                embedding=None
            )
        
        try:
            pil_image = Image.open(BytesIO(contents))
            
            if pil_image.mode != 'RGB':
                pil_image = pil_image.convert('RGB')
            
            image_array = np.array(pil_image)
            
            # Validate image dimensions
            if image_array.size == 0:
                return AddUserResponse(
                    is_saved=False,
                    user_name=user_name,
                    message="Invalid image: empty image array",
                    embedding=None
                )
            
            print(f"Processing image for user '{user_name}' with shape: {image_array.shape}")
            
        except Exception as e:
            print(f'Image processing error: {str(e)}')
            return AddUserResponse(
                is_saved=False,
                user_name=user_name,
                message=f"Failed to process image: {str(e)}",
                embedding=None
            )
        
        # Generate embeddings
        try:
            embeddings = generate_face_embedding(image_array)
            print(f"Generated embeddings for user '{user_name}' - type: {type(embeddings)}, length: {len(embeddings) if embeddings is not None else 'None'}")
            
            if embeddings is None:
                return AddUserResponse(
                    is_saved=False,
                    user_name=user_name,
                    message="Failed to generate embeddings - no face detected or invalid image",
                    embedding=None
                )
            
            # Convert to list format
            if isinstance(embeddings, np.ndarray):
                embeddings_list = embeddings.tolist()
            else:
                embeddings_list = embeddings
            
            # Validate embedding format
            if not isinstance(embeddings_list, list) or len(embeddings_list) == 0:
                return AddUserResponse(
                    is_saved=False,
                    user_name=user_name,
                    message="Invalid embedding format generated",
                    embedding=None
                )
            
            return AddUserResponse(
                is_saved=True,
                user_name=user_name,
                message=f"User '{user_name}' embeddings generated successfully",
                embedding=embeddings_list
            )
            
        except Exception as e:
            print(f'Embedding generation error: {str(e)}')
            return AddUserResponse(
                is_saved=False,
                user_name=user_name,
                message=f"Failed to generate embeddings: {str(e)}",
                embedding=None
            )
    
    except HTTPException:
        raise
    except Exception as e:
        print(f'Unexpected error in add_user: {str(e)}')
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An error occurred during processing: {str(e)}"
        )

@app.post("/authenticate", response_model=AuthenticateResponse)
async def authenticate_user(
    image: str = Form(..., description="Base64 encoded image data"),
    known_face_embeddings: str = Form(..., description="JSON dictionary of known face embeddings with user_name:embedding pairs"),
    threshold: float = Form(0.6, description="Distance threshold for authentication")
) -> AuthenticateResponse:
    """Authenticate a user by comparing the uploaded image against known face embeddings."""
    try:
        # Validate JSON input
        try:
            known_faces_dict = json.loads(known_face_embeddings)
            if not isinstance(known_faces_dict, dict):
                raise ValueError("Expected dictionary format")
            # Validate that all values are lists (embeddings)
            for user_name, embedding in known_faces_dict.items():
                if not isinstance(embedding, list):
                    raise ValueError(f"Embedding for user {user_name} must be a list")
        except (json.JSONDecodeError, ValueError) as ve:
            print(f'JSON Validation Error: {ve}')
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid format: {str(ve)}. Expected JSON dictionary with user_name:embedding pairs"
            )
        
        # Validate and decode image
        try:
            frame = decode_base64_image(image)
        except HTTPException:
            raise
        except Exception as e:
            print(f'Image Decoding Error: {str(e)}')
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid image data: {str(e)}"
            )
        
        # Authenticate user
        is_authenticated, user_name = await authenticator.authenticate(
            frame,
            known_face_embedding_dict=known_faces_dict,
            threshold=threshold
        )
        
        return AuthenticateResponse(
            is_authenticated=is_authenticated,
            user_name=user_name if is_authenticated else None
        )
    
    except HTTPException:
        raise
    except Exception as e:
        print(f'Authentication Processing Error: {str(e)}')
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An error occurred during authentication: {str(e)}"
        )

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)