from __future__ import annotations

from contextlib import asynccontextmanager
import logging
import time
import uuid

from fastapi.exceptions import RequestValidationError
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from kg_api.config import settings

from kg_api.extractor import extract_document
from kg_api.neo4j_client import Neo4jClient, neo4j_error_to_message
from kg_api.schemas import DeleteFileResponse, OkResponse, QaDocDetail, QaSearchItem, UpsertFileRequest, UpsertFileResponse


logger = logging.getLogger("uvicorn.error")



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
            logger.exception("neo4j schema init failed: %s", e)
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
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    req_id = uuid.uuid4().hex[:10]
    logger.exception(
        "unhandled exception (req_id=%s) %s %s",
        req_id,
        request.method,
        str(request.url),
        exc_info=exc,
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"ok": False, "message": f"internal error (req_id={req_id})"},
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
    req_id = uuid.uuid4().hex[:10]
    try:
        t0 = time.perf_counter()
        heading_aliases = None
        if isinstance(payload.metadata, dict):
            raw = payload.metadata.get("field_mapping") or payload.metadata.get("heading_aliases")
            if isinstance(raw, dict):
                heading_aliases = {str(k): str(v) for k, v in raw.items()}

        metadata_keys = sorted([str(k) for k in payload.metadata.keys()]) if isinstance(payload.metadata, dict) else None
        logger.info(
            "files.upsert start (req_id=%s) file_id=%s file_name=%s content_len=%s metadata_keys=%s",
            req_id,
            payload.file_id,
            payload.file_name,
            len(payload.content or ""),
            metadata_keys,
        )

        extracted = extract_document(
            file_name=payload.file_name,
            content=payload.content,
            llm=None,
            heading_aliases=heading_aliases,
        )
        t1 = time.perf_counter()
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
        t2 = time.perf_counter()
        logger.info(
            "files.upsert ok (req_id=%s) entities=%s relations=%s extract_ms=%s upsert_ms=%s total_ms=%s",
            req_id,
            len(entities),
            len(relations),
            int((t1 - t0) * 1000),
            int((t2 - t1) * 1000),
            int((t2 - t0) * 1000),
        )
        data = UpsertFileResponse(
            file_id=payload.file_id,
            entities_count=len(entities),
            relations_count=len(relations),
        )
        return OkResponse(ok=True, message="upserted", data=data.model_dump())
    except Exception as e:
        msg = neo4j_error_to_message(e)
        logger.exception(
            "files.upsert failed (req_id=%s) file_id=%s file_name=%s err=%s",
            req_id,
            getattr(payload, "file_id", None),
            getattr(payload, "file_name", None),
            e,
        )
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"{msg} (req_id={req_id})")


@app.delete("/files/{file_id}", response_model=OkResponse, tags=["files"], dependencies=[Depends(auth_dependency)])
def delete_file(file_id: str, cleanup_orphans: bool = True, client: Neo4jClient = Depends(neo4j)) -> OkResponse:
    req_id = uuid.uuid4().hex[:10]
    try:
        logger.info("files.delete start (req_id=%s) file_id=%s cleanup_orphans=%s", req_id, file_id, cleanup_orphans)
        deleted = client.delete_document_graph(doc_id=file_id, cleanup_orphans=cleanup_orphans)
        if not deleted:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="file not found")
        data = DeleteFileResponse(file_id=file_id, deleted=True)
        logger.info("files.delete ok (req_id=%s) file_id=%s", req_id, file_id)
        return OkResponse(ok=True, message="deleted", data=data.model_dump())
    except HTTPException:
        raise
    except Exception as e:
        msg = neo4j_error_to_message(e)
        logger.exception("files.delete failed (req_id=%s) file_id=%s err=%s", req_id, file_id, e)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"{msg} (req_id={req_id})")


@app.get("/files/{file_id}", response_model=OkResponse, tags=["files"], dependencies=[Depends(auth_dependency)])
def get_file(file_id: str, client: Neo4jClient = Depends(neo4j)) -> OkResponse:
    data = client.get_document_summary(doc_id=file_id)
    if not data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="file not found")
    return OkResponse(ok=True, message="ok", data=data)

# dify用的两个接口
# 用关键词检索候选文档，返回 doc_id
@app.get("/qa/search", response_model=OkResponse, tags=["qa"], dependencies=[Depends(auth_dependency)])
def qa_search(query: str, top_k: int = 5, client: Neo4jClient = Depends(neo4j)) -> OkResponse:
    try:
        results = client.search_documents(query=query, top_k=top_k)
        items = [QaSearchItem(**r).model_dump() for r in results]
        return OkResponse(ok=True, message="ok", data={"query": query, "top_k": min(max(int(top_k), 1), 20), "results": items})
    except Exception as e:
        msg = neo4j_error_to_message(e)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=msg)

# 拿某个 doc_id 的结构化详情
@app.get("/qa/doc/{doc_id}", response_model=OkResponse, tags=["qa"], dependencies=[Depends(auth_dependency)])
def qa_doc_detail(doc_id: str, client: Neo4jClient = Depends(neo4j)) -> OkResponse:
    try:
        detail = client.get_document_detail(doc_id=doc_id)
        if not detail:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="file not found")
        data = QaDocDetail(**detail).model_dump()
        return OkResponse(ok=True, message="ok", data=data)
    except HTTPException:
        raise
    except Exception as e:
        msg = neo4j_error_to_message(e)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=msg)
