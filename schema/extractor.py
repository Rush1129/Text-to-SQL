"""
Schema Extractor Service
========================

Extracts database schema from any PostgreSQL database and builds
ChromaDB vector embeddings for table-schema retrieval.

Refactored from a standalone script into importable functions that
support multi-database connections.

Key design: **schema caching** — if a connection already has a
cached ``schema_json`` in the ``db_connections`` table, the schema
is loaded from cache instead of re-extracting.  This means when a
user logs out and reconnects to the same database, the schema is
available instantly.

Public surface
--------------
    extract_schema       – Connect to a PG database and extract full schema
    build_embeddings     – Create/replace ChromaDB collection from schema
    extract_and_embed    – Convenience wrapper: extract + embed
    get_collection_name  – Deterministic collection name for a connection
"""

from __future__ import annotations

import json
import logging
import os

import chromadb
from dotenv import load_dotenv
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import PromptTemplate
from langchain_groq import ChatGroq
from sqlalchemy import create_engine, inspect

load_dotenv()

logger = logging.getLogger("schema_extractor")

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


# =========================================================
# LLM SETUP (shared)
# =========================================================

_llm = ChatGroq(model="openai/gpt-oss-20b", temperature=0)

_description_prompt = PromptTemplate(
    input_variables=["table_info"],
    template=(
        "Given the following database table schema, write a concise "
        "1-2 sentence description of what this table stores and its "
        "purpose. Return ONLY the description, nothing else.\n\n"
        "{table_info}"
    ),
)

_description_chain = _description_prompt | _llm | StrOutputParser()


# =========================================================
# COLLECTION NAME HELPER
# =========================================================

def get_collection_name(connection_id: str) -> str:
    """
    Return a deterministic ChromaDB collection name for a connection.

    Format: ``conn_{connection_id_without_dashes}`` (max 63 chars).
    """
    clean_id = connection_id.replace("-", "")[:50]
    return f"conn_{clean_id}"


# =========================================================
# EXTRACT SCHEMA
# =========================================================

def extract_schema(
    pg_url: str,
    generate_descriptions: bool = True,
) -> dict:
    """
    Connect to a PostgreSQL database and extract the full schema.

    Parameters
    ----------
    pg_url : str
        SQLAlchemy PostgreSQL URL, e.g.
        ``postgresql+psycopg2://user:pass@host:5432/dbname``
    generate_descriptions : bool
        If True, use the LLM to generate a 1-2 sentence description
        for each table.  Set to False for faster extraction.

    Returns
    -------
    dict
        Mapping of ``table_name`` → ``{columns, primary_keys,
        foreign_keys, description}``.
    """
    logger.info("Extracting schema from: %s", pg_url.split("@")[-1])

    engine = create_engine(pg_url)
    inspector = inspect(engine)
    tables = inspector.get_table_names()

    schema: dict = {}

    for table in tables:
        # Skip internal/system tables
        if table.startswith("pg_") or table == "audit_log":
            continue

        schema[table] = {
            "columns": [],
            "primary_keys": [],
            "foreign_keys": [],
        }

        # Columns
        for col in inspector.get_columns(table):
            schema[table]["columns"].append({
                "name": col["name"],
                "type": str(col["type"]),
                "nullable": col.get("nullable", True),
            })

        # Primary keys
        pk = inspector.get_pk_constraint(table)
        if pk and pk.get("constrained_columns"):
            schema[table]["primary_keys"] = pk["constrained_columns"]

        # Foreign keys
        for fk in inspector.get_foreign_keys(table):
            schema[table]["foreign_keys"].append({
                "constrained_columns": fk.get("constrained_columns", []),
                "referred_table": fk.get("referred_table"),
                "referred_columns": fk.get("referred_columns", []),
            })

    logger.info("Extracted %d tables.", len(schema))

    # Generate LLM descriptions
    if generate_descriptions and schema:
        logger.info("Generating table descriptions via LLM...")
        for table_name, table_info in schema.items():
            try:
                col_list = ", ".join(
                    f"{c['name']} ({c['type']})"
                    for c in table_info["columns"]
                )
                pk_list = ", ".join(table_info["primary_keys"]) or "None"
                fk_list = "; ".join(
                    f"{fk['constrained_columns']} -> "
                    f"{fk['referred_table']}.{fk['referred_columns']}"
                    for fk in table_info["foreign_keys"]
                ) or "None"

                table_summary = (
                    f"Table: {table_name}\n"
                    f"Columns: {col_list}\n"
                    f"Primary Keys: {pk_list}\n"
                    f"Foreign Keys: {fk_list}"
                )

                description = _description_chain.invoke({
                    "table_info": table_summary,
                })
                schema[table_name]["description"] = description.strip()

            except Exception as exc:
                logger.warning(
                    "Failed to generate description for %s: %s",
                    table_name, exc,
                )
                schema[table_name]["description"] = ""

        logger.info("Table descriptions generated.")

    engine.dispose()
    return schema


# =========================================================
# BUILD EMBEDDINGS
# =========================================================

def build_embeddings(
    schema: dict,
    collection_name: str,
    chroma_path: str | None = None,
) -> int:
    """
    Create or replace a ChromaDB collection with schema embeddings.

    Parameters
    ----------
    schema : dict
        Schema dict from ``extract_schema()``.
    collection_name : str
        ChromaDB collection name (unique per connection).
    chroma_path : str, optional
        Path to ChromaDB persistence directory.
        Defaults to ``./chroma_db``.

    Returns
    -------
    int
        Number of tables embedded.
    """
    chroma_path = chroma_path or os.path.join(PROJECT_ROOT, "chroma_db")

    logger.info(
        "Building embeddings for %d tables into collection '%s'...",
        len(schema), collection_name,
    )

    chroma_client = chromadb.PersistentClient(path=chroma_path)

    # Delete existing collection to avoid stale data
    try:
        chroma_client.delete_collection(collection_name)
    except Exception:
        pass

    collection = chroma_client.get_or_create_collection(
        name=collection_name,
        metadata={"hnsw:space": "cosine"},
    )

    # Build retrieval documents
    doc_ids = []
    doc_texts = []
    doc_metadatas = []

    for table_name, table_info in schema.items():
        columns = [
            f"{c['name']} ({c['type']})"
            for c in table_info["columns"]
        ]
        primary_keys = table_info.get("primary_keys", [])
        description = table_info.get("description", "")

        document_text = (
            f"Table Name:\n{table_name}\n\n"
            f"Description:\n{description}\n\n"
            f"Columns:\n{', '.join(columns)}\n\n"
            f"Primary Keys:\n"
            f"{', '.join(primary_keys) if primary_keys else 'None'}\n\n"
            f"Relationships:\n"
        )

        for fk in table_info.get("foreign_keys", []):
            document_text += (
                f"\n{table_name}."
                f"{','.join(fk['constrained_columns'])}"
                f" -> "
                f"{fk['referred_table']}."
                f"{','.join(fk['referred_columns'])}"
            )

        doc_ids.append(table_name)
        doc_texts.append(document_text.strip())
        doc_metadatas.append({
            "table_name": table_name,
            "description": description,
        })

    if doc_ids:
        collection.upsert(
            ids=doc_ids,
            documents=doc_texts,
            metadatas=doc_metadatas,
        )

    logger.info(
        "%d tables embedded into collection '%s'.",
        len(doc_ids), collection_name,
    )
    return len(doc_ids)


# =========================================================
# CONVENIENCE WRAPPER
# =========================================================

def extract_and_embed(
    pg_url: str,
    collection_name: str,
    chroma_path: str | None = None,
    generate_descriptions: bool = True,
) -> dict:
    """
    Extract schema from a PostgreSQL database and build ChromaDB
    embeddings in one call.

    Returns the schema dict.
    """
    schema = extract_schema(pg_url, generate_descriptions=generate_descriptions)
    build_embeddings(schema, collection_name, chroma_path)
    return schema


# =========================================================
# VERIFY CONNECTION
# =========================================================

def verify_pg_connection(pg_url: str) -> tuple[bool, str]:
    """
    Test whether we can connect to the PostgreSQL database.

    Returns (success: bool, message: str).
    """
    try:
        engine = create_engine(pg_url)
        with engine.connect() as conn:
            conn.execute(__import__("sqlalchemy").text("SELECT 1"))
        engine.dispose()
        return True, "Connection successful."
    except Exception as exc:
        return False, str(exc)


# =========================================================
# STANDALONE MODE (backward compat)
# =========================================================

if __name__ == "__main__":
    import sys

    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")

    # Use the default database from .env
    _pg_user = os.environ.get("PG_ADMIN_USER", "postgres")
    _pg_password = os.environ.get("PG_ADMIN_PASSWORD", "")
    _pg_host = os.environ.get("PG_HOST", "localhost")
    _pg_port = os.environ.get("PG_PORT", "5432")
    _pg_db = os.environ.get("PG_DB", "college_2")

    _pg_url = (
        f"postgresql+psycopg2://{_pg_user}:{_pg_password}"
        f"@{_pg_host}:{_pg_port}/{_pg_db}"
    )

    print(f"\n📡 Extracting schema from {_pg_host}:{_pg_port}/{_pg_db}...")

    _schema = extract_schema(_pg_url)
    print(f"\n✅ Extracted {len(_schema)} tables.")
    print(json.dumps(_schema, indent=4))

    # Save to outputs/
    outputs_dir = os.path.join(PROJECT_ROOT, "outputs")
    os.makedirs(outputs_dir, exist_ok=True)
    with open(os.path.join(outputs_dir, "schema.json"), "w") as f:
        json.dump(_schema, f, indent=4)
    print("\nSchema saved to outputs/schema.json")

    # Build default embeddings
    count = build_embeddings(_schema, "table_schemas")
    print(f"\n✅ {count} tables embedded into ChromaDB collection 'table_schemas'")
    print("Done!")