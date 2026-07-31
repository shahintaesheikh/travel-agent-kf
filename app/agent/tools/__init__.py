"""Tool package — one file per tool, auto-discovered at import.

Each tool is a single file in this directory with a public async function
decorated with @register_tool(). The registry scans this package at import
time and collects all decorated functions.

This layout is what makes tool lanes parallelizable: each tool is its own
lane, its own file, and the registry finds them automatically.
"""
