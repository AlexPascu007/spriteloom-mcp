"""Console entry point; MCP protocol data is the only stdout output."""
import argparse
import asyncio
import os


def main():
    parser = argparse.ArgumentParser(description="SpriteLoom MCP server (stdio)")
    parser.add_argument("--engine-root", help="path to the spriteloom-mlx checkout")
    parser.add_argument("--output-dir", help="default directory for new PNG outputs")
    args = parser.parse_args()
    if args.engine_root:
        os.environ["SPRITELOOM_ENGINE_ROOT"] = args.engine_root
    if args.output_dir:
        os.environ["SPRITELOOM_OUTPUT_DIR"] = args.output_dir
    from spriteloom_mcp.server import main as serve
    asyncio.run(serve())
