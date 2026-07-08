"""Runs inside the offline container. Reads /in parquet, execs the (already
AST-gated) user compute(df), writes /out/candidate.parquet. Mounted read-only."""
import sys
import pandas as pd

if __name__ == "__main__":
    in_path, out_path = sys.argv[1], sys.argv[2]
    df = pd.read_parquet(in_path)
    ns: dict = {"pd": pd, "__builtins__": __builtins__}
    exec(open("/app/user_source.py").read(), ns)
    result = ns["compute"](df)
    pd.DataFrame({"candidate": result}).to_parquet(out_path)
