from sqlalchemy import create_engine, inspect
from langchain_groq import ChatGroq
from langchain_core.prompts import PromptTemplate
from langchain_core.output_parsers import StrOutputParser
from dotenv import load_dotenv
import chromadb
import json
import os

load_dotenv()

# ---------------------------------------------------
# DATABASE CONNECTION
# ---------------------------------------------------

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# PostgreSQL connection — reads from .env
_pg_user     = os.environ.get("PG_ADMIN_USER",     "postgres")
_pg_password = os.environ.get("PG_ADMIN_PASSWORD",  "")
_pg_host     = os.environ.get("PG_HOST",             "localhost")
_pg_port     = os.environ.get("PG_PORT",             "5432")
_pg_db       = os.environ.get("PG_DB",               "college_2")

engine = create_engine(
    f"postgresql+psycopg2://{_pg_user}:{_pg_password}"
    f"@{_pg_host}:{_pg_port}/{_pg_db}"
)

inspector = inspect(engine)

# ---------------------------------------------------
# LLM SETUP (Groq)
# ---------------------------------------------------

llm = ChatGroq(
    model="openai/gpt-oss-20b",
    temperature=0
)

description_prompt = PromptTemplate(
    input_variables=["table_info"],
    template=(
        "Given the following database table schema, write a concise "
        "1-2 sentence description of what this table stores and its "
        "purpose. Return ONLY the description, nothing else.\n\n"
        "{table_info}"
    )
)

parser = StrOutputParser()

description_chain = description_prompt | llm | parser

# ---------------------------------------------------
# EXTRACT TABLE NAMES
# ---------------------------------------------------

tables = inspector.get_table_names()

# ---------------------------------------------------
# CREATE SCHEMA DICTIONARY
# ---------------------------------------------------

schema = {}

# ---------------------------------------------------
# LOOP THROUGH EACH TABLE
# ---------------------------------------------------

for table in tables:

    # Initialize table structure
    schema[table] = {
        "columns": [],
        "primary_keys": [],
        "foreign_keys": []
    }

    # ---------------------------------------------------
    # EXTRACT COLUMNS
    # ---------------------------------------------------

    columns = inspector.get_columns(table)

    for col in columns:

        column_info = {
            "name": col["name"],
            "type": str(col["type"]),
            "nullable": col["nullable"]
        }

        schema[table]["columns"].append(column_info)

    # ---------------------------------------------------
    # EXTRACT PRIMARY KEYS
    # ---------------------------------------------------

    pk = inspector.get_pk_constraint(table)

    if pk and pk.get("constrained_columns"):

        schema[table]["primary_keys"] = pk[
            "constrained_columns"
        ]

    # ---------------------------------------------------
    # EXTRACT FOREIGN KEYS
    # ---------------------------------------------------

    foreign_keys = inspector.get_foreign_keys(table)

    for fk in foreign_keys:

        fk_info = {
            "constrained_columns": fk.get(
                "constrained_columns", []
            ),
            "referred_table": fk.get(
                "referred_table"
            ),
            "referred_columns": fk.get(
                "referred_columns", []
            )
        }

        schema[table]["foreign_keys"].append(
            fk_info
        )

# ---------------------------------------------------
# GENERATE TABLE DESCRIPTIONS VIA LLM
# ---------------------------------------------------

print("Generating table descriptions via LLM...")

for table_name, table_info in schema.items():

    col_list = ", ".join(
        f"{c['name']} ({c['type']})"
        for c in table_info["columns"]
    )

    pk_list = ", ".join(
        table_info["primary_keys"]
    ) or "None"

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

    description = description_chain.invoke({
        "table_info": table_summary
    })

    schema[table_name]["description"] = description.strip()

    # print(f"  ✓ {table_name}: {description.strip()}")

# ---------------------------------------------------
# PRINT SCHEMA
# ---------------------------------------------------

print("\n" + json.dumps(schema, indent=4))

# ---------------------------------------------------
# SAVE SCHEMA TO JSON FILE
# ---------------------------------------------------

outputs_dir = os.path.join(PROJECT_ROOT, "outputs")
os.makedirs(outputs_dir, exist_ok=True)

with open(os.path.join(outputs_dir, "schema.json"), "w") as f:

    json.dump(schema, f, indent=4)

print("\nSchema saved successfully!")

# ---------------------------------------------------
# BUILD RETRIEVAL DOCUMENTS
# ---------------------------------------------------

retrieval_documents = []

for table_name, table_info in schema.items():

    columns = []

    for col in table_info["columns"]:

        columns.append(
            f"{col['name']} ({col['type']})"
        )

    primary_keys = table_info["primary_keys"]

    description = table_info.get("description", "")

    document_text = f"""
Table Name:
{table_name}

Description:
{description}

Columns:
{', '.join(columns)}

Primary Keys:
{', '.join(primary_keys) if primary_keys else 'None'}

Relationships:
"""

    for fk in table_info["foreign_keys"]:

        document_text += (
            f"\n{table_name}."
            f"{','.join(fk['constrained_columns'])}"
            f" -> "
            f"{fk['referred_table']}."
            f"{','.join(fk['referred_columns'])}"
        )

    retrieval_documents.append({
        "table_name": table_name,
        "description": description,
        "content": document_text.strip()
    })

# ---------------------------------------------------
# SAVE RETRIEVAL DOCUMENTS
# ---------------------------------------------------

with open(
    os.path.join(outputs_dir, "retrieval_documents.json"),
    "w"
) as f:

    json.dump(
        retrieval_documents,
        f,
        indent=4
    )

print(
    "\nRetrieval documents saved successfully!"
)

# ---------------------------------------------------
# CREATE CHROMADB VECTOR EMBEDDINGS
# ---------------------------------------------------

print("\nCreating ChromaDB vector embeddings...")

chroma_client = chromadb.PersistentClient(
    path=os.path.join(PROJECT_ROOT, "chroma_db")
)

# Delete existing collection if it exists
# (to avoid stale data on re-runs)
try:
    chroma_client.delete_collection("table_schemas")
except Exception:
    pass

collection = chroma_client.get_or_create_collection(
    name="table_schemas",
    metadata={"hnsw:space": "cosine"}
)

# Upsert each table as a document
doc_ids = []
doc_texts = []
doc_metadatas = []

for doc in retrieval_documents:

    doc_ids.append(doc["table_name"])
    doc_texts.append(doc["content"])
    doc_metadatas.append({
        "table_name": doc["table_name"],
        "description": doc["description"]
    })

collection.upsert(
    ids=doc_ids,
    documents=doc_texts,
    metadatas=doc_metadatas
)

print(
    f"\n✓ {len(doc_ids)} tables embedded into "
    f"ChromaDB collection 'table_schemas'"
)
print("  Persisted at: ./chroma_db/")
print("\nDone!")