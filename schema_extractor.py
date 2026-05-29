from sqlalchemy import create_engine, inspect
import json

# ---------------------------------------------------
# DATABASE CONNECTION
# ---------------------------------------------------

db_path = "college_2.sqlite"

engine = create_engine(f"sqlite:///{db_path}")

inspector = inspect(engine)

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
# PRINT SCHEMA
# ---------------------------------------------------

print(json.dumps(schema, indent=4))

# ---------------------------------------------------
# SAVE TO JSON FILE
# ---------------------------------------------------

with open("schema.json", "w") as f:

    json.dump(schema, f, indent=4)

print("\nSchema saved successfully!")