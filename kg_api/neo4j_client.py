from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from neo4j import GraphDatabase
from neo4j.exceptions import Neo4jError


logger = logging.getLogger("uvicorn.error")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_STOPWORDS = {
    "如何",
    "怎么",
    "怎么样",
    "怎样",
    "办理",
    "申请",
    "需要",
    "材料",
    "流程",
    "条件",
    "要求",
    "哪些",
    "什么",
    "我想",
    "我要",
    "帮我",
    "一下",
    "一下吧",
    "请问",
}


def _tokenize_query(query: str) -> list[str]:
    q = (query or "").strip().lower()
    if not q:
        return []
    parts = re.split(r"[\s,，。；;、/\\|]+", q)
    tokens: list[str] = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if p in _STOPWORDS:
            continue
        if len(p) == 1:
            continue
        tokens.append(p)
    uniq: list[str] = []
    seen: set[str] = set()
    for t in tokens:
        if t in seen:
            continue
        seen.add(t)
        uniq.append(t)
    return uniq[:12]


class Neo4jClient:
    def __init__(self, *, uri: str, user: str, password: str, database: str | None):
        self._driver = GraphDatabase.driver(uri, auth=(user, password))
        self._database = database

    def close(self) -> None:
        self._driver.close()

    def ensure_schema(self) -> None:
        statements = [
            "CREATE CONSTRAINT document_id IF NOT EXISTS FOR (d:Document) REQUIRE d.id IS UNIQUE",
            "CREATE CONSTRAINT entity_name IF NOT EXISTS FOR (e:Entity) REQUIRE e.name IS UNIQUE",
            "CREATE CONSTRAINT serviceitem_name IF NOT EXISTS FOR (s:ServiceItem) REQUIRE s.name IS UNIQUE",
            "CREATE CONSTRAINT material_name IF NOT EXISTS FOR (m:Material) REQUIRE m.name IS UNIQUE",
            "CREATE CONSTRAINT law_title IF NOT EXISTS FOR (l:Law) REQUIRE l.title IS UNIQUE",
            "CREATE CONSTRAINT org_name IF NOT EXISTS FOR (o:Organization) REQUIRE o.name IS UNIQUE",
            "CREATE CONSTRAINT location_address IF NOT EXISTS FOR (a:Location) REQUIRE a.address IS UNIQUE",
            "CREATE CONSTRAINT step_key IF NOT EXISTS FOR (st:Step) REQUIRE (st.doc_id, st.index) IS UNIQUE",
        ]
        with self._driver.session(database=self._database) as session:
            for cypher in statements:
                try:
                    session.run(cypher).consume()
                except Neo4jError as e:
                    logger.exception("neo4j schema statement failed: %s", cypher)

    def ping(self) -> None:
        with self._driver.session(database=self._database) as session:
            session.run("RETURN 1").consume()

    def search_documents(self, *, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        top_k = int(top_k)
        if top_k <= 0:
            top_k = 5
        if top_k > 20:
            top_k = 20

        query_lower = (query or "").strip().lower()
        tokens = _tokenize_query(query_lower)
        if not tokens and query_lower:
            tokens = [query_lower]

        cypher = """
        MATCH (d:Document)
        OPTIONAL MATCH (d)-[:DESCRIBES]->(s:ServiceItem)
        OPTIONAL MATCH (s)-[:HANDLED_BY {doc_id: d.id}]->(o:Organization)
        OPTIONAL MATCH (s)-[:HANDLED_AT {doc_id: d.id}]->(loc:Location)
        OPTIONAL MATCH (s)-[:REQUIRES {doc_id: d.id}]->(m:Material)
        OPTIONAL MATCH (s)-[:BASED_ON {doc_id: d.id}]->(l:Law)
        OPTIONAL MATCH (s)-[:HAS_STEP {doc_id: d.id}]->(st:Step)
        OPTIONAL MATCH (d)-[:MENTIONS]->(e:Entity)
        WITH d, s,
             collect(distinct o.name) AS orgs,
             collect(distinct loc.address) AS addrs,
             collect(distinct m.name) AS materials,
             collect(distinct l.title) AS laws,
             collect(distinct st.index) AS step_indexes_raw,
             collect(distinct e.name) AS entities
        WITH d, s,
             head([x IN orgs WHERE x IS NOT NULL]) AS organization,
             head([x IN addrs WHERE x IS NOT NULL]) AS address,
             [x IN materials WHERE x IS NOT NULL] AS materials,
             [x IN laws WHERE x IS NOT NULL] AS laws,
             [x IN step_indexes_raw WHERE x IS NOT NULL] AS step_indexes,
             [x IN entities WHERE x IS NOT NULL] AS entities
        WITH d, s, organization, address, materials, laws, step_indexes, entities,
             toLower(coalesce(d.name, "")) AS dn,
             toLower(coalesce(d.content, "")) AS dc,
             toLower(coalesce(s.name, "")) AS sn,
             toLower(coalesce(organization, "")) AS on,
             toLower(coalesce(address, "")) AS an
        WITH d, s, organization, address, materials, laws, step_indexes, entities,
             reduce(sc = 0.0, t IN $tokens |
               sc
               + CASE WHEN sn CONTAINS t THEN 6.0 ELSE 0.0 END
               + CASE WHEN dn CONTAINS t THEN 4.0 ELSE 0.0 END
               + CASE WHEN any(x IN materials WHERE toLower(x) CONTAINS t) THEN 2.0 ELSE 0.0 END
               + CASE WHEN any(x IN laws WHERE toLower(x) CONTAINS t) THEN 1.5 ELSE 0.0 END
               + CASE WHEN on CONTAINS t THEN 1.5 ELSE 0.0 END
               + CASE WHEN an CONTAINS t THEN 1.5 ELSE 0.0 END
               + CASE WHEN any(x IN entities WHERE toLower(x) CONTAINS t) THEN 1.0 ELSE 0.0 END
               + CASE WHEN dc CONTAINS t THEN 0.5 ELSE 0.0 END
             ) AS score,
             dn, sn, dc, on, an
        WHERE score > 0 OR ($query_lower <> "" AND (sn CONTAINS $query_lower OR dn CONTAINS $query_lower OR dc CONTAINS $query_lower))
        WITH d, s, organization, address, materials, laws, step_indexes,
             CASE WHEN score > 0 THEN score ELSE 0.1 END AS score2,
             [f IN [
               CASE WHEN sn CONTAINS $query_lower THEN "service.name" END,
               CASE WHEN dn CONTAINS $query_lower THEN "document.name" END,
               CASE WHEN on CONTAINS $query_lower THEN "organization" END,
               CASE WHEN an CONTAINS $query_lower THEN "address" END
             ] WHERE f IS NOT NULL] AS matched_fields
        RETURN d.id AS doc_id,
               d.name AS file_name,
               d.updated_at AS updated_at,
               s.name AS service_name,
               organization AS organization,
               address AS address,
               size(materials) AS materials_count,
               size(step_indexes) AS steps_count,
               size(laws) AS laws_count,
               score2 AS score,
               matched_fields AS matched_fields
        ORDER BY score DESC, updated_at DESC
        LIMIT $top_k
        """
        params = {"tokens": tokens, "query_lower": query_lower, "top_k": top_k}
        with self._driver.session(database=self._database) as session:
            records = session.run(cypher, params)
            out: list[dict[str, Any]] = []
            for r in records:
                out.append(
                    {
                        "doc_id": r["doc_id"],
                        "file_name": r["file_name"],
                        "service_name": r["service_name"],
                        "organization": r["organization"],
                        "address": r["address"],
                        "updated_at": r["updated_at"],
                        "materials_count": int(r["materials_count"] or 0),
                        "steps_count": int(r["steps_count"] or 0),
                        "laws_count": int(r["laws_count"] or 0),
                        "score": float(r["score"] or 0.0),
                        "matched_fields": list(r["matched_fields"] or []),
                    }
                )
            return out

    def get_document_detail(self, *, doc_id: str) -> dict[str, Any] | None:
        cypher = """
        MATCH (d:Document {id: $doc_id})
        OPTIONAL MATCH (d)-[:DESCRIBES]->(s:ServiceItem)
        CALL {
          WITH s
          OPTIONAL MATCH (s)-[:REQUIRES {doc_id: $doc_id}]->(m:Material)
          RETURN collect(distinct m.name) AS materials
        }
        CALL {
          WITH s
          OPTIONAL MATCH (s)-[:HAS_STEP {doc_id: $doc_id}]->(st:Step)
          WITH st ORDER BY st.index ASC
          RETURN collect(st.text) AS steps
        }
        CALL {
          WITH s
          OPTIONAL MATCH (s)-[:BASED_ON {doc_id: $doc_id}]->(l:Law)
          RETURN collect(distinct l.title) AS laws
        }
        CALL {
          WITH d
          OPTIONAL MATCH (d)-[:MENTIONS]->(e:Entity)
          RETURN collect(distinct e.name) AS entities
        }
        CALL {
          WITH s
          OPTIONAL MATCH (s)-[:HANDLED_BY {doc_id: $doc_id}]->(o:Organization)
          RETURN head(collect(distinct o.name)) AS organization
        }
        CALL {
          WITH s
          OPTIONAL MATCH (s)-[:HANDLED_AT {doc_id: $doc_id}]->(loc:Location)
          RETURN head(collect(distinct loc.address)) AS address
        }
        RETURN d.id AS doc_id,
               d.name AS file_name,
               d.updated_at AS updated_at,
               s.name AS service_name,
               organization AS organization,
               address AS address,
               materials AS materials,
               steps AS steps,
               laws AS laws,
               entities AS entities
        """
        with self._driver.session(database=self._database) as session:
            r = session.run(cypher, doc_id=doc_id).single()
            if not r:
                return None
            return {
                "doc_id": r["doc_id"],
                "file_name": r["file_name"],
                "updated_at": r["updated_at"],
                "service_name": r["service_name"],
                "organization": r["organization"],
                "address": r["address"],
                "materials": list(r["materials"] or []),
                "steps": list(r["steps"] or []),
                "laws": list(r["laws"] or []),
                "entities": list(r["entities"] or []),
            }

    def upsert_document_graph(
        self,
        *,
        doc_id: str,
        doc_name: str,
        content: str,
        metadata: dict[str, Any] | None,
        structured: dict[str, Any] | None,
        entities: list[str],
        relations: list[tuple[str, str, str]],
    ) -> None:
        metadata_json = json.dumps(metadata, ensure_ascii=False) if metadata is not None else None
        structured_json = json.dumps(structured, ensure_ascii=False) if structured is not None else None

        values = (structured or {}).get("values") if isinstance(structured, dict) else None
        if not isinstance(values, dict):
            values = {}

        service_name = str(values.get("service_item") or "").strip() or doc_name
        materials = values.get("materials") if isinstance(values.get("materials"), list) else []
        materials = [str(x).strip() for x in materials if isinstance(x, str) and str(x).strip()]

        process = values.get("process") if isinstance(values.get("process"), list) else []
        steps = [{"index": idx + 1, "text": str(x).strip()} for idx, x in enumerate(process) if isinstance(x, str) and str(x).strip()]

        policy_basis = values.get("policy_basis") if isinstance(values.get("policy_basis"), list) else []
        laws = [str(x).strip() for x in policy_basis if isinstance(x, str) and str(x).strip()]

        organization = str(values.get("organization") or "").strip() or None
        address = str(values.get("address") or "").strip() or None

        materials_preview = materials[:3]
        if len(materials) > len(materials_preview):
            materials_preview = materials_preview + [f"...(+{len(materials) - len(materials_preview)})"]
        logger.info(
            "neo4j upsert graph doc_id=%s service_name=%s materials=%s steps=%s laws=%s organization=%s address=%s materials_preview=%s",
            doc_id,
            service_name,
            len(materials),
            len(steps),
            len(laws),
            organization,
            address,
            materials_preview,
        )

        cypher = """
        MERGE (d:Document {id: $doc_id})
        SET d.name = $doc_name,
            d.content = $content,
            d.metadata_json = $metadata_json,
            d.structured_json = $structured_json,
            d.updated_at = $updated_at
        WITH d
        OPTIONAL MATCH (d)-[m:MENTIONS]->(:Entity)
        DELETE m
        WITH d
        OPTIONAL MATCH (d)-[ds:DESCRIBES]->(:ServiceItem)
        DELETE ds
        WITH d
        OPTIONAL MATCH ()-[r]->() WHERE r.doc_id = $doc_id
        DELETE r
        WITH d
        OPTIONAL MATCH (st:Step {doc_id: $doc_id})
        DETACH DELETE st
        WITH d
        FOREACH (entity_name IN $entities |
          MERGE (e:Entity {name: entity_name})
          MERGE (d)-[:MENTIONS]->(e)
        )
        FOREACH (rel IN $relations |
          MERGE (a:Entity {name: rel.source})
          MERGE (b:Entity {name: rel.target})
          MERGE (a)-[:RELATED {doc_id: $doc_id, type: rel.type}]->(b)
        )
        MERGE (s:ServiceItem {name: $service_name})
        MERGE (d)-[:DESCRIBES]->(s)
        WITH d, s
        FOREACH (material_name IN $materials |
          MERGE (m:Material {name: material_name})
          MERGE (s)-[:REQUIRES {doc_id: $doc_id}]->(m)
        )
        FOREACH (step IN $steps |
          MERGE (st:Step {doc_id: $doc_id, index: step.index})
          SET st.text = step.text
          MERGE (s)-[:HAS_STEP {doc_id: $doc_id, index: step.index}]->(st)
        )
        FOREACH (law_title IN $laws |
          MERGE (l:Law {title: law_title})
          MERGE (s)-[:BASED_ON {doc_id: $doc_id}]->(l)
        )
        FOREACH (_ IN CASE WHEN $organization IS NULL THEN [] ELSE [1] END |
          MERGE (o:Organization {name: $organization})
          MERGE (s)-[:HANDLED_BY {doc_id: $doc_id}]->(o)
        )
        WITH d, s
        FOREACH (_ IN CASE WHEN $address IS NULL THEN [] ELSE [1] END |
          MERGE (loc:Location {address: $address})
          MERGE (s)-[:HANDLED_AT {doc_id: $doc_id}]->(loc)
        )
        """
        rel_dicts = [{"source": a, "type": t, "target": b} for (a, t, b) in relations]
        params = {
            "doc_id": doc_id,
            "doc_name": doc_name,
            "content": content,
            "metadata_json": metadata_json,
            "structured_json": structured_json,
            "updated_at": _utc_now_iso(),
            "entities": entities,
            "relations": rel_dicts,
            "service_name": service_name,
            "materials": materials,
            "steps": steps,
            "laws": laws,
            "organization": organization,
            "address": address,
        }
        with self._driver.session(database=self._database) as session:
            session.execute_write(lambda tx: tx.run(cypher, params).consume())

    def delete_document_graph(self, *, doc_id: str, cleanup_orphans: bool = True) -> bool:
        exists = False
        with self._driver.session(database=self._database) as session:
            result = session.run("MATCH (d:Document {id: $doc_id}) RETURN d.id AS id", doc_id=doc_id)
            exists = result.single() is not None
            if not exists:
                return False

            delete_cypher = """
            MATCH (d:Document {id: $doc_id})
            DETACH DELETE d
            WITH $doc_id AS doc_id
            OPTIONAL MATCH ()-[r]->() WHERE r.doc_id = doc_id
            DELETE r
            WITH doc_id
            OPTIONAL MATCH (st:Step {doc_id: doc_id})
            DETACH DELETE st
            """
            session.execute_write(lambda tx: tx.run(delete_cypher, doc_id=doc_id).consume())

            if cleanup_orphans:
                cleanup_cypher = """
                MATCH (n)
                WHERE (n:Entity OR n:ServiceItem OR n:Material OR n:Law OR n:Organization OR n:Location OR n:Step)
                  AND NOT (n)--()
                DELETE n
                """
                session.execute_write(lambda tx: tx.run(cleanup_cypher).consume())

        return True

    def get_document_summary(self, *, doc_id: str) -> dict[str, Any] | None:
        cypher = """
        MATCH (d:Document {id: $doc_id})
        OPTIONAL MATCH (d)-[:DESCRIBES]->(s:ServiceItem)
        OPTIONAL MATCH (d)-[:MENTIONS]->(e:Entity)
        WITH d, s, collect(distinct e.name) AS entities
        OPTIONAL MATCH ()-[r:RELATED {doc_id: $doc_id}]->()
        WITH d, s, entities, count(r) AS relations_count
        RETURN d.id AS id, d.name AS name, d.updated_at AS updated_at,
               s.name AS service_name,
               entities AS entities,
               relations_count AS relations_count
        """
        with self._driver.session(database=self._database) as session:
            record = session.run(cypher, doc_id=doc_id).single()
            if not record:
                return None
            return {
                "file_id": record["id"],
                "file_name": record["name"],
                "updated_at": record["updated_at"],
                "service_name": record["service_name"],
                "entities": record["entities"],
                "relations_count": record["relations_count"],
            }


def neo4j_error_to_message(err: Exception) -> str:
    if isinstance(err, Neo4jError):
        code = getattr(err, "code", None) or "Neo4jError"
        message = getattr(err, "message", None) or str(err) or ""
        message = message.replace("\r", " ").replace("\n", " ").strip()
        if len(message) > 500:
            message = message[:500] + "..."
        if message:
            return f"neo4j error: {code}: {message}"
        return f"neo4j error: {code}"
    message = str(err).strip()
    if message:
        if len(message) > 500:
            message = message[:500] + "..."
        return message
    return f"{type(err).__name__}"
