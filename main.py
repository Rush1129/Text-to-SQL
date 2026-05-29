import json
from langchain_core.prompts import PromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_groq import ChatGroq
from dotenv import load_dotenv

load_dotenv()
# =========================================================
# LOAD SCHEMA
# =========================================================

with open("outputs/schema.json", "r") as f:
    schema = json.load(f)

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
# RELEVANT TABLE SELECTION
# =========================================================

def get_relevant_tables(question):

    question = question.lower()

    relevant_tables = []

    for table_name in schema.keys():

        if table_name.lower() in question:
            relevant_tables.append(table_name)

    # fallback if nothing matched
    if not relevant_tables:
        relevant_tables = list(schema.keys())[:3]

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
1. Generate ONLY SQL query.
2. Do NOT explain anything.
3. Use ONLY tables and columns provided.
4. Generate syntactically correct SQLite SQL.
5. Use proper JOINS using relationships.
6. If the question is ambiguous, do not guess. The application will ask
   for clarification before this prompt is sent.

Database Schema:
{schema_text}

{relationship_text}

{example_text}

User Question:
{user_question}

SQL:
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
# CREATE CHAIN
# =========================================================

chain = (
    prompt_template
    | llm
    | parser
)

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

    final_prompt = build_prompt(
        user_question
    )

    sql_output = chain.invoke({
        "prompt": final_prompt
    })

    print("\nGenerated SQL:\n")
    print(sql_output)
