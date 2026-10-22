"""Run real stdio MCP requests. Requires a configured engine and model cache."""
import asyncio
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from PIL import Image, ImageChops

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'output' / 'validation'

def data(result):
    if result.isError:
        raise RuntimeError(str(result.content))
    return json.loads(next(c.text for c in result.content if c.type == 'text'))

async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, SPRITELOOM_ENGINE_ROOT=str(ROOT.parent / 'spriteloom-mlx'))
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / 'mcp_entry.py')], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=600)) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            assert len(tools) == 7
            assert all('context' not in t.inputSchema.get('properties', {}) for t in tools)
            print('Tools:', ', '.join(t.name for t in tools), flush=True)
            print('Initial status:', data(await session.call_tool('spriteloom_status')), flush=True)
            async def call(name, args):
                result = await session.call_tool(name, dict(args, output_dir=str(OUT)))
                value = data(result)
                assert any(c.type == 'image' for c in result.content)
                print(name, json.dumps(value), flush=True)
                return value
            generated = await call('generate_sprite', {'prompt':'A small green robot with a square head and orange eyes, front view, pixel art game sprite', 'seed':42})
            source = generated['paths'][0]
            before = hashlib.sha256(Path(source).read_bytes()).hexdigest()
            await call('edit_sprite', {'image_path':source,'prompt':'Change the robot body to bright blue. Keep its shape and orange eyes.', 'preserve_palette':False, 'seed':43})
            mask = Image.new('L', Image.open(source).size, 0)
            mask.paste(255, (20, 10, 44, 32))
            mask_path = OUT / 'mask.png'; mask.save(mask_path)
            painted = await call('inpaint_sprite', {'image_path':source,'mask_path':str(mask_path),'prompt':'Give the robot a red visor on its head', 'preserve_palette':False,'seed':44})
            a = Image.open(source).convert('RGBA'); b = Image.open(painted['paths'][0]).convert('RGBA')
            difference = ImageChops.difference(a, b)
            outside = ImageChops.invert(mask)
            assert all(channel.getbbox() is None for channel in (ImageChops.multiply(band, outside) for band in difference.split()))
            await call('rotate_sprite', {'image_path':source,'subject':'green robot with a square head and orange eyes','view':'left side profile','seed':45})
            assert hashlib.sha256(Path(source).read_bytes()).hexdigest() == before
            print('Stop:', data(await session.call_tool('stop_service')), flush=True)
            print('PASS: schemas, previews, four modes, source hash, exact mask, managed shutdown', flush=True)

if __name__ == '__main__':
    asyncio.run(main())
