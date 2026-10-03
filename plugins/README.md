# SALU tool plugins

Drop a `.py` file here to add tools without changing SALU's core. Files starting
with `_` are ignored. Plugins run inside SALU with your user's permissions, so
only add code you have read and trust.

```python
# plugins/hello.py
from salu.tools import ToolResult, salu_tool

@salu_tool("hello", "Say hello to someone.", risk="low", category="demo",
           properties={"name": {"type": "string"}}, required=["name"])
async def hello(args, ctx):
    return ToolResult.success(f"Hello, {args['name']}!")

TOOLS = [hello]
```

Every tool needs a name, description, JSON input schema, and risk level
(`low` / `medium` / `high`). The permission system enforces the risk level
automatically: plugins can't skip confirmation.
