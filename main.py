import json
import chromadb
from langchain_core.prompts import PromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_groq import ChatGroq
from dotenv import load_dotenv
from structured_models import (
    StructuredSQLResponse,
    validate_sql_syntax,
)
from sql_guardrails import SQLGuardrail
from sandbox_executor import SandboxExecutor

load_dotenv()
# =========================================================
# LOAD SCHEMA
# =========================================================

with open("outputs/schema.json", "r") as f:
    schema = json.load(f)

# =========================================================
# LOAD CHROMADB COLLECTION
# =========================================================

chroma_client = chromadb.PersistentClient(
    path="./chroma_db"
)

collection = chroma_client.get_collection(
    name="table_schemas"
)

# =========================================================
# LOAD FEW-SHOT EXAMPLES
# =========================================================

try:
    with open("examples/examples.json", "r") as f:
        examples = json.load(f)

except FileNotFoundError:
    examples = []

# =========================================================
# GROQ LLM SETUP
# =========================================================

# Set your GROQ API key before running:
# export GROQ_API_KEY="your_api_key"

llm = ChatGroq(
    model="openai/gpt-oss-20b",
    temperature=0
)

# =========================================================
# STRUCTURED OUTPUT LLM (for SQL generation)
# =========================================================

structured_llm = llm.with_structured_output(
    StructuredSQLResponse
)

# =========================================================
# GUARDRAIL MIDDLEWARE
# =========================================================

guardrail = SQLGuardrail()  # uses default config

# =========================================================
# SANDBOX EXECUTOR
# =========================================================

sandbox = SandboxExecutor(
    db_path="database/college_2.sqlite",
    readonly=True,
)

# =========================================================
# RELEVANT TABLE SELECTION
# =========================================================

def get_relevant_tables(question, top_k=3):

    results = collection.query(
        query_texts=[question],
        n_results=top_k
    )

    # Extract table names from metadata
    relevant_tables = [
        m["table_name"]
        for m in results["metadatas"][0]
    ]

    return relevant_tables

# =========================================================
# FORMAT SCHEMA
# =========================================================

def format_schema(relevant_tables):

    formatted_schema = ""

    for table in relevant_tables:

        table_info = schema[table]

        formatted_schema += f"\nTable: {table}\n"

        formatted_schema += "Columns:\n"

        for col in table_info["columns"]:

            formatted_schema += (
                f"- {col['name']} "
                f"({col['type']})\n"
            )

        if table_info["primary_keys"]:

            formatted_schema += "Primary Keys:\n"

            for pk in table_info["primary_keys"]:

                formatted_schema += f"- {pk}\n"

    return formatted_schema

# =========================================================
# FORMAT RELATIONSHIPS
# =========================================================

def format_relationships(relevant_tables):

    relationships = "\nRelationships:\n"

    for table in relevant_tables:

        table_info = schema[table]

        for fk in table_info["foreign_keys"]:

            constrained = fk["constrained_columns"]

            referred_table = fk["referred_table"]

            referred_cols = fk["referred_columns"]

            relationships += (
                f"{table}.{constrained} "
                f"→ "
                f"{referred_table}.{referred_cols}\n"
            )

    return relationships

# =========================================================
# FORMAT FEW-SHOT EXAMPLES
# =========================================================

def format_examples():

    if not examples:
        return ""

    formatted = "\nExamples:\n"

    for ex in examples:

        formatted += (
            f"\nQuestion: {ex['question']}\n"
        )

        formatted += (
            f"SQL: {ex['sql']}\n"
        )

    return formatted

# =========================================================
# AMBIGUITY JUDGE
# =========================================================

def extract_json_object(text):

    cleaned = text.strip()

    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").strip()

        if cleaned.startswith("json"):
            cleaned = cleaned[4:].strip()

    start = cleaned.find("{")
    end = cleaned.rfind("}")

    if start == -1 or end == -1:
        raise ValueError(
            f"LLM did not return a JSON object: {text}"
        )

    return json.loads(cleaned[start:end + 1])


def build_ambiguity_prompt(user_question):

    all_tables = list(schema.keys())

    schema_text = format_schema(
        all_tables
    )

    relationship_text = format_relationships(
        all_tables
    )

    example_text = format_examples()

    prompt = f"""
You are an ambiguity judge for a text-to-SQL system.

Your job is to decide whether the user's question has multiple plausible
SQL interpretations given the database schema, relationships, and examples.

Return ONLY valid JSON. Do not wrap it in markdown.

Use this exact JSON shape:
{{
  "is_ambiguous": true,
  "clarification_request": {{
    "type": "clarification_request",
    "reason": "ambiguous_user_question",
    "ambiguous_term": "short phrase that caused ambiguity",
    "message": "Ask the user to clarify the intended interpretation.",
    "interpretations": [
      {{
        "label": "snake_case_label",
        "description": "What this interpretation means.",
        "example_query": "A clarified natural language question."
      }}
    ],
    "original_question": "The original user question."
  }}
}}

If the question is not ambiguous, return:
{{
  "is_ambiguous": false,
  "clarification_request": null
}}

Guidelines:
1. Mark ambiguous only when two or more materially different SQL queries
   are reasonable for the same question.
2. Do not mark broad but clear requests as ambiguous.
3. Do not guess business definitions. For example, if a metric could mean
   gross, net, active, enrolled, attempted, completed, current, or historical,
   ask for clarification.
4. List each plausible interpretation with an example clarified query.
5. Use only concepts supported by the schema and examples, unless the user's
   wording introduces an external business metric that needs clarification.

Database Schema:
{schema_text}

{relationship_text}

{example_text}

User Question:
{user_question}
"""

    return prompt


def detect_ambiguity(user_question):

    ambiguity_prompt = build_ambiguity_prompt(
        user_question
    )

    judge_output = chain.invoke({
        "prompt": ambiguity_prompt
    })

    try:
        parsed_output = extract_json_object(
            judge_output
        )
    except (json.JSONDecodeError, ValueError):
        return {
            "type": "clarification_request",
            "reason": "ambiguity_judge_invalid_response",
            "ambiguous_term": "unknown",
            "message": (
                "I could not reliably determine whether the question is "
                "ambiguous. Please rephrase with the exact metric, entity, "
                "time period, and filters you want."
            ),
            "interpretations": [
                {
                    "label": "rephrased_question",
                    "description": (
                        "A more specific version of the question with the "
                        "intended meaning made explicit."
                    ),
                    "example_query": (
                        "Count enrolled students by department for Fall 2010."
                    ),
                }
            ],
            "original_question": user_question,
        }

    if parsed_output.get("is_ambiguous"):
        return parsed_output.get("clarification_request")

    return None

# =========================================================
# BUILD FINAL PROMPT
# =========================================================

def build_prompt(user_question):

    relevant_tables = get_relevant_tables(
        user_question
    )

    schema_text = format_schema(
        relevant_tables
    )

    relationship_text = format_relationships(
        relevant_tables
    )

    example_text = format_examples()

    prompt = f"""
You are an expert SQLite SQL generator.

Rules:
1. Use ONLY tables and columns provided in the schema.
2. Generate syntactically correct SQLite SQL.
3. Use proper JOINS using the relationships provided.
4. If the question is ambiguous, do not guess. The application will ask
   for clarification before this prompt is sent.
5. For the confidence_score, rate your confidence from 0.0 to 1.0 that
   the generated SQL correctly answers the user's question.
6. In tables_accessed, list every table name referenced in the query.
7. In columns_accessed, list every column with its table name.
8. In explanation, provide a clear natural language description of what
   the SQL query does.

Database Schema:
{schema_text}

{relationship_text}

{example_text}

User Question:
{user_question}
"""

    return prompt

# =========================================================
# PROMPT TEMPLATE
# =========================================================

prompt_template = PromptTemplate(
    input_variables=["prompt"],
    template="{prompt}"
)

# =========================================================
# OUTPUT PARSER
# =========================================================

parser = StrOutputParser()

# =========================================================
# CREATE CHAIN (used by ambiguity judge)
# =========================================================

chain = (
    prompt_template
    | llm
    | parser
)

# =========================================================
# STRUCTURED SQL GENERATION
# =========================================================

def generate_structured_sql(user_question):
    """Generate SQL with structured output and validate."""

    prompt_text = build_prompt(user_question)

    response = structured_llm.invoke(prompt_text)

    is_valid, validation_msg = validate_sql_syntax(
        response.sql_query
    )

    return response, is_valid, validation_msg

# =========================================================
# MAIN LOOP
# =========================================================

while True:

    user_question = input(
        "\nEnter your question (or type exit): "
    )

    if user_question.lower() == "exit":
        break

    clarification_request = detect_ambiguity(
        user_question
    )

    if clarification_request:
        print("\nClarification Required:\n")
        print(json.dumps(
            clarification_request,
            indent=2
        ))
        continue

    response, is_valid, validation_msg = (
        generate_structured_sql(user_question)
    )

    # ── Guardrail Validation ────────────────────
    guardrail_result = guardrail.validate(
        response.sql_query,
        db_path="database/college_2.sqlite",
    )

    if not guardrail_result.allowed:
        print("\n" + "=" * 55)
        print("  🚫 QUERY BLOCKED BY GUARDRAILS")
        print("=" * 55)
        print(f"\n📝 Original Query:\n{response.sql_query}")
        print("\n⛔ Violations:")
        for v in guardrail_result.violations:
            print(f"   • {v}")
        print("\n" + "=" * 55)
        continue

    # Use the guardrail-modified SQL (may have LIMIT)
    safe_sql = guardrail_result.sql

    # ── Display Results ─────────────────────────
    print("\n" + "=" * 55)
    print("  STRUCTURED SQL RESPONSE")
    print("=" * 55)

    print(f"\n📝 SQL Query:\n{safe_sql}")

    if safe_sql != response.sql_query:
        print(
            "\n🔒 Guardrail: LIMIT clause was "
            "automatically appended."
        )

    print(f"\n💬 Explanation:\n{response.explanation}")

    print(
        f"\n🎯 Confidence Score: "
        f"{response.confidence_score:.0%}"
    )

    if response.confidence_score < 0.7:
        print(
            "   ⚠️  Low confidence — review the "
            "query carefully before using."
        )

    print("\n📊 Tables Accessed:")
    for table in response.tables_accessed:
        print(f"   • {table}")

    print("\n📋 Columns Accessed:")
    for col in response.columns_accessed:
        print(f"   • {col.table}.{col.column}")

    # ── SQL Validation ──────────────────────────
    print("\n🔍 SQL Validation:")
    if is_valid:
        print(f"   ✅ {validation_msg}")
    else:
        print(f"   ❌ {validation_msg}")

    # ── Sandbox Execution ───────────────────────
    print("\n" + "-" * 55)
    print("  🔒 SANDBOX EXECUTION")
    print("-" * 55)

    sandbox_result = sandbox.execute(safe_sql)

    print(f"\n🛡️  Protection: {sandbox_result.sandbox_info}")

    if sandbox_result.success:
        print(
            f"\n✅ Query executed successfully "
            f"({sandbox_result.row_count} row(s) returned)"
        )

        if sandbox_result.columns:
            # ── Column Headers ──────────────────
            header = " | ".join(
                f"{col:<20}" for col in sandbox_result.columns
            )
            print(f"\n   {header}")
            print(f"   {'-' * len(header)}")

            # ── Data Rows ───────────────────────
            display_limit = 20
            for row in sandbox_result.rows[:display_limit]:
                row_str = " | ".join(
                    f"{str(val):<20}"
                    for val in row
                )
                print(f"   {row_str}")

            if sandbox_result.row_count > display_limit:
                remaining = (
                    sandbox_result.row_count - display_limit
                )
                print(
                    f"\n   ... and {remaining} more row(s)"
                )
    else:
        print(
            f"\n❌ Sandbox blocked execution: "
            f"{sandbox_result.error}"
        )

    print("\n" + "=" * 55)
