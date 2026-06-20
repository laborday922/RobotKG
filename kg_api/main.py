from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi.exceptions import RequestValidationError
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from kg_api.config import settings

from kg_api.extractor import extract_document
from kg_api.neo4j_client import Neo4jClient, neo4j_error_to_message
from kg_api.schemas import DeleteFileResponse, OkResponse, UpsertFileRequest, UpsertFileResponse




def _require_token(authorization: str | None) -> None:
    if not settings.api_token:
        return
    if not authorization:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing token")
    prefix = "Bearer "
    if not authorization.startswith(prefix):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token")
    token = authorization[len(prefix) :].strip()
    if token != settings.api_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token")


def auth_dependency(authorization: str | None = Header(default=None)) -> None:
    _require_token(authorization)


@asynccontextmanager
async def lifespan(app: FastAPI):
    client = Neo4jClient(
        uri=settings.neo4j_uri,
        user=settings.neo4j_user,
        password=settings.neo4j_password,
        database=settings.neo4j_database,
    )
    init_error: str | None = None
    if settings.neo4j_create_schema:
        try:
            client.ensure_schema()
        except Exception as e:
            init_error = str(e)
    app.state.neo4j = client
    app.state.neo4j_init_error = init_error
    try:
        yield
    finally:
        client.close()


app = FastAPI(title="RobotKG API", version="0.1.0", lifespan=lifespan)

@app.exception_handler(HTTPException)
async def http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"ok": False, "message": str(exc.detail)},
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"ok": False, "message": "validation error", "detail": exc.errors()},
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(_: Request, __: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"ok": False, "message": "internal error"},
    )



def neo4j() -> Neo4jClient:
    return app.state.neo4j

@app.get("/health", response_model=OkResponse, tags=["health"])
def health() -> OkResponse:
    return OkResponse(ok=True, message="ok")


@app.get("/ready", response_model=OkResponse, tags=["health"])
def ready(client: Neo4jClient = Depends(neo4j)) -> OkResponse:
    init_error = getattr(app.state, "neo4j_init_error", None)
    if init_error:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=init_error)
    try:
        client.ping()
        return OkResponse(ok=True, message="ok")
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))


@app.post("/files/upsert", response_model=OkResponse, tags=["files"], dependencies=[Depends(auth_dependency)])
def upsert_file(payload: UpsertFileRequest, client: Neo4jClient = Depends(neo4j)) -> OkResponse:
    try:
        heading_aliases = None
        if isinstance(payload.metadata, dict):
            raw = payload.metadata.get("field_mapping") or payload.metadata.get("heading_aliases")
            if isinstance(raw, dict):
                heading_aliases = {str(k): str(v) for k, v in raw.items()}

        extracted = extract_document(
            file_name=payload.file_name,
            content=payload.content,
            llm=None,
            heading_aliases=heading_aliases,
        )
        entities = sorted(extracted.graph.entities)
        relations = extracted.graph.relations
        client.upsert_document_graph(
            doc_id=payload.file_id,
            doc_name=payload.file_name,
            content=payload.content,
            metadata=payload.metadata,
            structured=extracted.structured,
            entities=entities,
            relations=relations,
        )
        data = UpsertFileResponse(
            file_id=payload.file_id,
            entities_count=len(entities),
            relations_count=len(relations),
        )
        return OkResponse(ok=True, message="upserted", data=data.model_dump())
    except Exception as e:
        msg = neo4j_error_to_message(e)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=msg)


@app.delete("/files/{file_id}", response_model=OkResponse, tags=["files"], dependencies=[Depends(auth_dependency)])
def delete_file(file_id: str, cleanup_orphans: bool = True, client: Neo4jClient = Depends(neo4j)) -> OkResponse:
    try:
        deleted = client.delete_document_graph(doc_id=file_id, cleanup_orphans=cleanup_orphans)
        if not deleted:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="file not found")
        data = DeleteFileResponse(file_id=file_id, deleted=True)
        return OkResponse(ok=True, message="deleted", data=data.model_dump())
    except HTTPException:
        raise
    except Exception as e:
        msg = neo4j_error_to_message(e)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=msg)


@app.get("/files/{file_id}", response_model=OkResponse, tags=["files"], dependencies=[Depends(auth_dependency)])
def get_file(file_id: str, client: Neo4jClient = Depends(neo4j)) -> OkResponse:
    data = client.get_document_summary(doc_id=file_id)
    if not data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="file not found")
    return OkResponse(ok=True, message="ok", data=data)
