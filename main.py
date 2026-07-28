import os
import re
import json
import base64
import httpx
from fastmcp import FastMCP
from mcp.types import TextContent, ImageContent
 
mcp = FastMCP("小红书阅读器")
 
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.0 Mobile/15E148 Safari/604.1"
)
 
HEADERS = {
    "User-Agent": MOBILE_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
 
 
async def fetch_page(url: str) -> str:
    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        resp = await client.get(url, headers=HEADERS)
        resp.raise_for_status()
        return resp.text
 
 
def extract_state(html: str) -> dict | None:
    match = re.search(
        r"window\.__INITIAL_STATE__\s*=\s*(\{.+?\})\s*</script>",
        html,
        re.DOTALL,
    )
    if not match:
        return None
    raw = match.group(1)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raw = re.sub(r"\bundefined\b", "null", raw)
        try:
            return json.loads(raw)
        except Exception:
            return None
 
 
def find_note(state: dict) -> dict | None:
    candidates = [
        ["noteData", "data", "noteData"],
        ["normalNotePreloadData", "noteData"],
    ]
    for path in candidates:
        node = state
        try:
            for k in path:
                node = node[k]
            if node:
                return node
        except (KeyError, TypeError):
            continue
    return None
 
 
@mcp.tool()
async def xhs_peek(url: str) -> list:
    """
    读取一篇小红书笔记的全部内容，包括标题、正文和图片。
    请传入 app 内「分享 → 复制链接」得到的链接（xhslink.com 短链最稳定）。
    """
    try:
        html = await fetch_page(url)
    except Exception as e:
        return [TextContent(type="text", text=f"❌ 请求失败：{e}")]
 
    state = extract_state(html)
    if not state:
        return [TextContent(type="text", text="❌ 无法解析页面数据，请确认链接有效（建议用 app 内分享的短链）")]
 
    note = find_note(state)
    if not note:
        return [TextContent(type="text", text="❌ 笔记结构解析失败，可能页面已更新～")]
 
    title = note.get("title", "（无标题）")
    desc = note.get("desc", "（无正文）")
    author = note.get("user", {}).get("nickname", "未知作者")
    interact = note.get("interactInfo", {})
    liked = interact.get("likedCount", "?")
    collected = interact.get("collectedCount", "?")
    commented = interact.get("commentCount", "?")
 
    text_block = (
        f"🍠 小红书笔记\n\n"
        f"📌 {title}\n"
        f"👤 {author}\n"
        f"❤️ {liked} 点赞  ⭐ {collected} 收藏  💬 {commented} 评论\n\n"
        f"{desc}"
    )
 
    result = [TextContent(type="text", text=text_block)]
 
    # 获取图片（最多 6 张）
    image_list = note.get("imageList", [])
    async with httpx.AsyncClient(follow_redirects=True, timeout=20) as client:
        for img_info in image_list[:6]:
            img_url = None
            for info in img_info.get("infoList", []):
                if info.get("imageScene") == "WB_DFT":
                    img_url = info.get("url")
                    break
            if not img_url:
                img_url = img_info.get("url") or img_info.get("urlDefault")
 
            if img_url:
                try:
                    r = await client.get(
                        img_url, headers={"User-Agent": MOBILE_UA}, timeout=15
                    )
                    if r.status_code == 200:
                        b64 = base64.standard_b64encode(r.content).decode()
                        result.append(
                            ImageContent(type="image", data=b64, mimeType="image/jpeg")
                        )
                except Exception:
                    pass
 
    if not image_list:
        result.append(TextContent(type="text", text="📹 这是视频笔记，暂只获取了文字内容。"))
 
    return result
 
 
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    mcp.run(transport="sse", port=port, host="0.0.0.0")
 


