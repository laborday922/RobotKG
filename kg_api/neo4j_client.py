from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from neo4j import GraphDatabase
from neo4j.exceptions import Neo4jError


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
        ]
        with self._driver.session(database=self._database) as session:
            for cypher in statements:
                session.run(cypher).consume()

    def ping(self) -> None:
        with self._driver.session(database=self._database) as session:
            session.run("RETURN 1").consume()

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
        OPTIONAL MATCH ()-[r:RELATED {doc_id: $doc_id}]->()
        DELETE r
        WITH d
        UNWIND $entities AS entity_name
        MERGE (e:Entity {name: entity_name})
        MERGE (d)-[:MENTIONS]->(e)
        WITH d
        UNWIND $relations AS rel
        MERGE (a:Entity {name: rel.source})
        MERGE (b:Entity {name: rel.target})
        MERGE (a)-[:RELATED {doc_id: $doc_id, type: rel.type}]->(b)
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
            OPTIONAL MATCH (d)-[m:MENTIONS]->()
            DELETE m
            WITH d
            OPTIONAL MATCH ()-[r:RELATED {doc_id: $doc_id}]->()
            DELETE r
            WITH d
            DETACH DELETE d
            """
            session.execute_write(lambda tx: tx.run(delete_cypher, doc_id=doc_id).consume())

            if cleanup_orphans:
                cleanup_cypher = "MATCH (e:Entity) WHERE NOT (e)--() DELETE e"
                session.execute_write(lambda tx: tx.run(cleanup_cypher).consume())

        return True

    def get_document_summary(self, *, doc_id: str) -> dict[str, Any] | None:
        cypher = """
        MATCH (d:Document {id: $doc_id})
        OPTIONAL MATCH (d)-[:MENTIONS]->(e:Entity)
        WITH d, collect(distinct e.name) AS entities
        OPTIONAL MATCH ()-[r:RELATED {doc_id: $doc_id}]->()
        WITH d, entities, count(r) AS relations_count
        RETURN d.id AS id, d.name AS name, d.updated_at AS updated_at, entities AS entities, relations_count AS relations_count
        """
        with self._driver.session(database=self._database) as session:
            record = session.run(cypher, doc_id=doc_id).single()
            if not record:
                return None
            return {
                "file_id": record["id"],
                "file_name": record["name"],
                "updated_at": record["updated_at"],
                "entities": record["entities"],
                "relations_count": record["relations_count"],
            }


def neo4j_error_to_message(err: Exception) -> str:
    if isinstance(err, Neo4jError):
        code = getattr(err, "code", None) or "Neo4jError"
        return f"neo4j error: {code}"
    return "neo4j error"
