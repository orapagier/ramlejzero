import os
import sys
import importlib
import traceback

TOOLS = {}

def get_tool_definitions() -> list:
    return [t["definition"] for t in TOOLS.values()]

def reload_tools():
    global TOOLS
    TOOLS.clear()
    
    tools_dir = os.path.dirname(__file__)
    if not os.path.exists(tools_dir):
        return
        
    for filename in os.listdir(tools_dir):
        if filename.endswith(".py") and filename != "__init__.py":
            module_name = filename[:-3]
            try:
                # Reload if already loaded to pick up changes
                if f"tools.{module_name}" in sys.modules:
                    module = importlib.reload(sys.modules[f"tools.{module_name}"])
                else:
                    module = importlib.import_module(f"tools.{module_name}")
                    
                if hasattr(module, "TOOL_DEFINITION"):
                    tool_def = getattr(module, "TOOL_DEFINITION")
                    name = tool_def.get("name")
                    if name:
                        TOOLS[name] = {
                            "definition": tool_def,
                            "module": module_name
                        }
            except Exception as e:
                print(f"Error loading tool {module_name}: {e}")
                traceback.print_exc()

# Initial load
reload_tools()
