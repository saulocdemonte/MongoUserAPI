from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, EmailStr, Field
from typing import List, Optional, Any
from datetime import date
import os
from motor.motor_asyncio import AsyncIOMotorClient # Usando Motor para MongoDB assíncrono
from bson import ObjectId
from pydantic_core import core_schema # Necessário para a nova implementação de PyObjectId

# --- 1. Configuração da Aplicação FastAPI ---
app = FastAPI(
    title="MongoUserAPI",
    description="API RESTful para gerenciamento de usuários com MongoDB e FastAPI.",
    version="1.0.0"
)

# --- 2. Configuração do Banco de Dados MongoDB ---
MONGO_DETAILS = os.getenv("MONGO_DETAILS", "mongodb://localhost:27017")
client = None
db = None

# Função para conectar ao MongoDB
def connect_to_mongo():
    global client, db
    try:
        client = AsyncIOMotorClient(MONGO_DETAILS) # Usando AsyncIOMotorClient
        db = client.users_db
        print("Conectado ao MongoDB com sucesso!")
    except Exception as e:
        print(f"Erro ao conectar ao MongoDB: {e}")

def close_mongo_connection():
    global client
    if client:
        client.close()
        print("Conexão com MongoDB fechada.")

@app.on_event("startup")
async def startup_db_client():
    connect_to_mongo()

@app.on_event("shutdown")
async def shutdown_db_client():
    close_mongo_connection()

# --- 3. Modelagem de Dados (Pydantic) ---
class PyObjectId(ObjectId):
    @classmethod
    def __get_pydantic_core_schema__(cls, _source_type: Any, _handler: Any) -> core_schema.CoreSchema:
        # Define como o Pydantic deve tratar ObjectId para JSON e Python
        return core_schema.json_or_python_schema(
            json_schema=core_schema.str_schema(), # No JSON, será uma string
            python_schema=core_schema.union_schema([ # No Python, pode ser ObjectId ou string
                core_schema.is_instance_schema(ObjectId),
                core_schema.chain_schema([
                    core_schema.str_schema(),
                    core_schema.no_info_plain_validator_function(cls.validate) # Usa nosso validador
                ])
            ])
        )

    @classmethod
    def validate(cls, v: Any) -> ObjectId:
        if isinstance(v, ObjectId):
            return v
        if isinstance(v, str) and ObjectId.is_valid(v):
            return ObjectId(v)
        raise ValueError("ID de objeto inválido")

# Modelo base para um usuário
class UserBase(BaseModel):
    name: str = Field(..., min_length=3, max_length=50, description="Nome completo do usuário")
    email: EmailStr = Field(..., description="Endereço de e-mail único do usuário")
    birth_date: date = Field(..., description="Data de nascimento do usuário (formato YYYY-MM-DD)")

    class Config:
        arbitrary_types_allowed = True # Permite que o Pydantic use tipos personalizados como PyObjectId
        json_schema_extra = { # Usando json_schema_extra para evitar o aviso
            "example": {
                "name": "João Silva",
                "email": "joao.silva@example.com",
                "birth_date": "1990-01-15"
            }
        }

class UserCreate(UserBase):
    pass

class UserUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=3, max_length=50, description="Novo nome do usuário")
    email: Optional[EmailStr] = Field(None, description="Novo endereço de e-mail único do usuário")
    birth_date: Optional[date] = Field(None, description="Nova data de nascimento do usuário (formato YYYY-MM-DD)")

    class Config:
        json_schema_extra = {
            "example": {
                "name": "João Victor",
                "email": "joao.victor@example.com"
            }
        }

class UserInDB(UserBase):
    # default_factory=PyObjectId é uma boa prática para gerar IDs automaticamente se não fornecidos
    id: PyObjectId = Field(default_factory=PyObjectId, alias="_id", description="ID único do usuário no MongoDB")

    class Config:
        populate_by_name = True # Permite que o Pydantic mapeie '_id' para 'id'
        arbitrary_types_allowed = True
        json_encoders = {ObjectId: str} # Converte ObjectId para string ao serializar para JSON
        json_schema_extra = {
            "example": {
                "id": "60c72b2f9b1d4c001f8e4d6a",
                "name": "Maria Souza",
                "email": "maria.souza@example.com",
                "birth_date": "1985-05-20"
            }
        }

# --- 4. Rotas da API (CRUD) ---

@app.get("/", summary="Verifica o status da API", response_description="Mensagem de boas-vindas da API")
async def read_root():
    return {"message": "Bem-vindo à MongoUserAPI! Acesse /docs para a documentação interativa."}

@app.post(
    "/users/",
    response_model=UserInDB,
    status_code=status.HTTP_201_CREATED,
    summary="Cria um novo usuário",
    response_description="O usuário recém-criado com seu ID do MongoDB"
)
async def create_user(user: UserCreate):
    # Verifica se já existe um usuário com o mesmo e-mail
    if await db.users.find_one({"email": user.email}):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Já existe um usuário com este e-mail."
        )
    user_dict = user.model_dump(by_alias=True, exclude_none=True) # Use model_dump para Pydantic V2
    result = await db.users.insert_one(user_dict)
    created_user = await db.users.find_one({"_id": result.inserted_id})
    if created_user:
        return UserInDB(**created_user)
    raise HTTPException(status_code=500, detail="Erro ao recuperar usuário criado.")


@app.get(
    "/users/",
    response_model=List[UserInDB],
    summary="Lista todos os usuários",
    response_description="Lista de todos os usuários cadastrados"
)
async def list_users():
    users = []
    # Use to_list() para iterar sobre o cursor assíncrono
    for user in await db.users.find().to_list(length=1000): # Limite de 1000 documentos para evitar sobrecarga
        users.append(UserInDB(**user))
    return users

@app.get(
    "/users/{user_id}",
    response_model=UserInDB,
    summary="Busca um usuário por ID",
    response_description="Detalhes do usuário encontrado"
)
async def get_user(user_id: str):
    if not ObjectId.is_valid(user_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="ID inválido")
    user = await db.users.find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Usuário não encontrado")
    return UserInDB(**user)

@app.put(
    "/users/{user_id}",
    response_model=UserInDB,
    summary="Atualiza um usuário",
    response_description="Usuário atualizado"
)
async def update_user(user_id: str, user_update: UserUpdate):
    if not ObjectId.is_valid(user_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="ID inválido")
    
    # Converte o modelo Pydantic para um dicionário, excluindo campos None
    update_data = user_update.model_dump(exclude_none=True) # Use model_dump para Pydantic V2
    
    if not update_data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Nenhum dado para atualizar.")
    
    result = await db.users.update_one({"_id": ObjectId(user_id)}, {"$set": update_data})
    
    if result.matched_count == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Usuário não encontrado")
    
    user = await db.users.find_one({"_id": ObjectId(user_id)})
    if user:
        return UserInDB(**user)
    raise HTTPException(status_code=500, detail="Erro ao recuperar usuário atualizado.")

@app.delete(
    "/users/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove um usuário",
    response_description="Usuário removido com sucesso"
)
async def delete_user(user_id: str):
    if not ObjectId.is_valid(user_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="ID inválido")
    
    result = await db.users.delete_one({"_id": ObjectId(user_id)})
    
    if result.deleted_count == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Usuário não encontrado")
    
    return None # Retorna None para 204 No Content
