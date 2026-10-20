"""Temporary isolated native-engine diagnostics for Windows qualification."""

import subprocess
import sys

CHILD = r'''
import faulthandler
import sys
faulthandler.dump_traceback_later(35, exit=True)
import ladybug
from pyrel import cypher
from pyrel.analysis import Analysis
from pyrel.code_graph import CodeGraph
from pyrel.model import Source
original_database = ladybug.Database
def database(**kwargs):
    kwargs["max_num_threads"] = int(sys.argv[1])
    return original_database(**kwargs)
ladybug.Database = database
original_execute = ladybug.Connection.execute
def execute(self, query, *args, **kwargs):
    print("execute:", str(query)[:100], flush=True)
    if sys.argv[2] == "text" and not isinstance(query, str):
        query = text
    result = original_execute(self, query, *args, **kwargs)
    print("executed", flush=True)
    return result
ladybug.Connection.execute = execute
graph = CodeGraph(Analysis([Source("a.py", "sample",
    "def leaf(): pass\ndef mid(): leaf()\ndef start(): mid()\n")]))
graph.materialize()
text = 'MATCH (a:Symbol {name:"sample.leaf"}) OPTIONAL MATCH (a)-[:CALLS]->(b) RETURN b.name'
if sys.argv[2] == "no-udf":
    cypher.install_diff = lambda *args: {}
print(cypher.run(graph, text), flush=True)
faulthandler.cancel_dump_traceback_later()
'''

for threads, mode in ((1, "prepared"), (2, "prepared"), (2, "text"), (2, "no-udf")):
    print(f"--- Native OPTIONAL MATCH: {threads} threads, {mode} ---", flush=True)
    try:
        result = subprocess.run([sys.executable, "-u", "-c", CHILD, str(threads), mode], timeout=45)
        print("exit:", result.returncode, flush=True)
    except subprocess.TimeoutExpired:
        print("Native query exceeded 45 seconds", flush=True)
