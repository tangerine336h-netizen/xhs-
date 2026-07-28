import os
import re
import json
import base64
import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, Mount
from starlette.middleware.cors import CORSMiddleware
from mcp.server.sse import SseServerTransport
from mcp.server import Server
from mcp.types import Tool, TextContent, ImageContent
 
# ── 小红书抓取逻辑 ────────────────────────────────────────────
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.0 Mobile/15E148 Safari/604.1"
)
HEADERS = {
    "User-Agent": MOBILE_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
}
 
async def fetch_page(url: str) -> str:
    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        resp = await client.get(url, headers=HEADERS)
        resp.raise_for_status()
        return resp.text
 
def extract_state(html: str):
    match = re.search(r"window\.__INITIAL_STATE__\s*=\s*(\{.+?\})\s*</script>", html, re.DOTALL)
    if not match:
        return None
    raw = match.group(1)
    try:
        return json.loads(raw)
    except Exception:
        raw = re.sub(r"\bundefined\b", "null", raw)
        try:
            return json.loads(raw)
        except Exception:
            return None
 
def find_note(state: dict):
    for path in [["noteData","data","noteData"], ["normalNotePreloadData","noteData"]]:
        node = state
        try:
            for k in path:
                node = node[k]
            if node:
                return node
        except Exception:
            continue
    return None
 
async def xhs_peek_impl(url: str) -> list:
    try:
        html = await fetch_page(url)
    except Exception as e:
        return [TextContent(type="text", text=f"❌ 请求失败：{e}")]
    state = extract_state(html)
    if not state:
        return [TextContent(type="text", text="❌ 无法解析页面，建议用 app 内分享的短链")]
    note = find_note(state)
    if not note:
        return [TextContent(type="text", text="❌ 笔记结构解析失败")]
 
    title = note.get("title", "（无标题）")
    desc  = note.get("desc", "（无正文）")
    author = note.get("user", {}).get("nickname", "未知")
    ia = note.get("interactInfo", {})
    text = (
        f"🍠 小红书笔记\n\n"
        f"📌 {title}\n👤 {author}\n"
        f"❤️ {ia.get('likedCount','?')} 点赞  "
        f"⭐ {ia.get('collectedCount','?')} 收藏  "
        f"💬 {ia.get('commentCount','?')} 评论\n\n{desc}"
    )
    result = [TextContent(type="text", text=text)]
 
    async with httpx.AsyncClient(follow_redirects=True, timeout=20) as client:
        for img_info in note.get("imageList", [])[:6]:
            img_url = None
            for info in img_info.get("infoList", []):
                if info.get("imageScene") == "WB_DFT":
                    img_url = info.get("url")
                    break
            if not img_url:
                img_url = img_info.get("url") or img_info.get("urlDefault")
            if img_url:
                try:
                    r = await client.get(img_url, headers={"User-Agent": MOBILE_UA}, timeout=15)
                    if r.status_code == 200:
                        b64 = base64.standard_b64encode(r.content).decode()
                        result.append(ImageContent(type="image", data=b64, mimeType="image/jpeg"))
                except Exception:
                    pass
    return result
 
# ── MCP 服务器 ────────────────────────────────────────────────
mcp_server = Server("小红书阅读器")
 
@mcp_server.list_tools()
async def list_tools():
    return [
        Tool(
            name="xhs_peek",
            description="读取小红书笔记内容（文字+图片）。传入 app 分享的链接即可。",
            inputSchema={
                "type": "object",
                "properties": {"url": {"type": "string", "description": "小红书笔记链接"}},
                "required": ["url"],
            },
        )
    ]
 
@mcp_server.call_tool()
async def call_tool(name: str, arguments: dict):
    if name == "xhs_peek":
        return await xhs_peek_impl(arguments["url"])
    raise ValueError(f"Unknown tool: {name}")
 
# ── OAuth shim（让 claude.ai 能连上来）────────────────────────
async def oauth_meta(request):
    base = str(request.base_url).rstrip("/")
    return JSONResponse({
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "client_credentials"],
        "code_challenge_methods_supported": ["S256"],
    })
 
async def oauth_register(request):
    body = await request.json()
    return JSONResponse({
        "client_id": "xhs-client",
        "client_secret": "xhs-secret",
        "client_name": body.get("client_name", "client"),
        "redirect_uris": body.get("redirect_uris", []),
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
    })
 
async def oauth_authorize(request):
    p = dict(request.query_params)
    redirect = p.get("redirect_uri", "")
    state = p.get("state", "")
    return Response(status_code=302,
                    headers={"Location": f"{redirect}?code=xhs-code&state={state}"})
 
async def oauth_token(request):
    return JSONResponse({"access_token": "xhs-token", "token_type": "bearer", "expires_in": 86400})
 
# ── SSE 路由 ─────────────────────────────────────────────────
sse_transport = SseServerTransport("/messages/")
 
async def handle_sse(request):
    async with sse_transport.connect_sse(
        request.scope, request.receive, request._send
    ) as (r, w):
        await mcp_server.run(r, w, mcp_server.create_initialization_options())
 
async def handle_messages(request):
    await sse_transport.handle_post_message(request.scope, request.receive, request._send)
 
# ── Starlette App ─────────────────────────────────────────────
app = Starlette(routes=[
    Route("/.well-known/oauth-authorization-server", oauth_meta),
    Route("/.well-known/openid-configuration", oauth_meta),
    Route("/oauth/register", oauth_register, methods=["POST"]),
    Route("/oauth/authorize", oauth_authorize),
    Route("/oauth/token", oauth_token, methods=["POST"]),
    Route("/sse", handle_sse),
    Mount("/messages/", routes=[Route("/", handle_messages, methods=["POST"])]),
])
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
 
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
 


