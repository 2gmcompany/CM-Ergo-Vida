import hashlib
import html
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlparse, parse_qs

import requests
from google.auth.transport.requests import AuthorizedSession
from google.oauth2.service_account import Credentials
from openai import OpenAI

ROOT = Path(__file__).resolve().parents[1]
BLOG = ROOT / "blog"
STATE_PATH = BLOG / ".agent-state.json"
POSTS_PATH = BLOG / "posts.json"
SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
MAPUTO = timezone(timedelta(hours=2))
INTERVAL = timedelta(hours=48)
HEADERS = {
    "timestamp": "Carimbo de data/hora",
    "title": "TITULO",
    "subtitle": "SUBTITULO",
    "text": "TEXTO",
    "images": "IMAGENS",
}


def read_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"last_check": None, "processed": []}


def sheets_rows(session):
    meta = session.get(
        f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}",
        params={"fields": "sheets.properties.title"}, timeout=30,
    )
    meta.raise_for_status()
    sheet_names = [s["properties"]["title"] for s in meta.json().get("sheets", [])]
    if not sheet_names:
        raise RuntimeError("A folha não contém separadores legíveis.")
    range_name = quote(f"'{sheet_names[0]}'!A:Z", safe="'!: ")
    response = session.get(
        f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/{range_name}",
        params={"valueRenderOption": "FORMATTED_VALUE"}, timeout=30,
    )
    response.raise_for_status()
    values = response.json().get("values", [])
    if not values:
        return [], []
    headers = [str(h).strip() for h in values[0]]
    missing = [h for h in HEADERS.values() if h not in headers]
    if missing:
        raise RuntimeError("Cabeçalhos em falta na folha: " + ", ".join(missing))
    rows = []
    for cells in values[1:]:
        padded = cells + [""] * (len(headers) - len(cells))
        rows.append(dict(zip(headers, padded)))
    return headers, rows


def row_id(row):
    raw = "\x1f".join(str(row.get(h, "")).strip() for h in HEADERS.values())
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def parse_timestamp(value):
    value = str(value).strip()
    for fmt in (
        "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M",
        "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M",
        "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M",
    ):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=MAPUTO)
        except ValueError:
            pass
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=MAPUTO) if parsed.tzinfo is None else parsed.astimezone(MAPUTO)
    except ValueError as exc:
        raise RuntimeError(f"Formato de carimbo de data/hora não reconhecido: {value}") from exc


def image_file_id(url):
    parsed = urlparse(url.strip())
    match = re.search(r"/d/([\w-]+)", parsed.path)
    if match:
        return match.group(1)
    return parse_qs(parsed.query).get("id", [None])[0]


def save_images(session, cell_value, stem):
    saved = []
    urls = re.findall(r"https?://[^\s,;]+", str(cell_value or ""))
    for index, url in enumerate(urls[:5], start=1):
        file_id = image_file_id(url)
        if not file_id:
            print(f"Ignorada imagem sem ID Drive: {url}")
            continue
        meta = session.get(
            f"https://www.googleapis.com/drive/v3/files/{file_id}",
            params={"fields": "name,mimeType"}, timeout=30,
        )
        meta.raise_for_status()
        info = meta.json()
        mime = info.get("mimeType", "")
        if not mime.startswith("image/"):
            print(f"Ignorado ficheiro que não é imagem: {info.get('name', file_id)}")
            continue
        download = session.get(
            f"https://www.googleapis.com/drive/v3/files/{file_id}",
            params={"alt": "media"}, timeout=60,
        )
        download.raise_for_status()
        if len(download.content) > 12 * 1024 * 1024:
            print(f"Imagem demasiado grande: {info.get('name', file_id)}")
            continue
        ext = Path(info.get("name", "imagem.jpg")).suffix.lower()
        if ext not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
            ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}.get(mime, ".jpg")
        image_dir = BLOG / "images"
        image_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{stem}-{index}{ext}"
        (image_dir / filename).write_bytes(download.content)
        saved.append("../images/" + filename)
    return saved


def draft(client, title, subtitle, source_text):
    response = client.chat.completions.create(
        model=os.getenv("OPENAI_MODEL", "gpt-6.1-terra"),
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": (
                "És editor do blog de um centro médico em Nampula. Escreve em português de Moçambique, "
                "com clareza e tom acolhedor. Reorganiza e melhora o texto fornecido sem inventar factos, "
                "diagnósticos, tratamentos, doses, estatísticas ou promessas de saúde. Se o conteúdo pedir "
                "aconselhamento clínico individual, limita-te a apresentar o tema de forma informativa. "
                "Devolve apenas JSON com as chaves title, subtitle e paragraphs; paragraphs deve ser uma lista de parágrafos em texto simples."
            )},
            {"role": "user", "content": json.dumps({"title": title, "subtitle": subtitle, "text": source_text}, ensure_ascii=False)},
        ],
    )
    data = json.loads(response.choices[0].message.content)
    paragraphs = data.get("paragraphs", [])
    if not data.get("title") or not data.get("subtitle") or not paragraphs:
        raise RuntimeError("OpenAI devolveu um artigo incompleto.")
    return data["title"].strip(), data["subtitle"].strip(), [str(p).strip() for p in paragraphs if str(p).strip()]


def slugify(value):
    import unicodedata
    ascii_text = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", ascii_text.lower())).strip("-")[:70] or "artigo-ergovida"


def publish_post(row, session, client, existing_slugs):
    source_title = str(row.get(HEADERS["title"], "")).strip()
    source_subtitle = str(row.get(HEADERS["subtitle"], "")).strip()
    source_text = str(row.get(HEADERS["text"], "")).strip()
    if not source_title or not source_subtitle or not source_text:
        print("Resposta ignorada: título, subtítulo ou texto vazio.")
        return None
    submitted = parse_timestamp(row.get(HEADERS["timestamp"], ""))
    title, subtitle, paragraphs = draft(client, source_title, source_subtitle, source_text)
    slug = slugify(title)
    suffix = 2
    while slug in existing_slugs or (BLOG / "posts" / f"{slug}.html").exists():
        slug = f"{slugify(title)}-{suffix}"
        suffix += 1
    existing_slugs.add(slug)
    images = save_images(session, row.get(HEADERS["images"], ""), slug)
    safe_title, safe_subtitle = html.escape(title), html.escape(subtitle)
    image_html = "".join(f'<figure><img src="{html.escape(src, quote=True)}" alt="{safe_title}" loading="lazy"></figure>' for src in images)
    body_html = "\n".join(f"<p>{html.escape(p).replace(chr(10), '<br>')}</p>" for p in paragraphs)
    date_text = submitted.strftime("%d/%m/%Y às %H:%M")
    post_path = BLOG / "posts" / f"{slug}.html"
    post_path.parent.mkdir(parents=True, exist_ok=True)
    page = f'''<!doctype html>
<html lang="pt-MZ"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="description" content="{safe_subtitle}"><link rel="stylesheet" href="../../css/styles.css"><title>{safe_title} | Blog Ergovida</title></head>
<body><main class="blog-article"><a href="../">← Voltar ao blog</a><span>Blog Ergovida</span><h1>{safe_title}</h1><p class="blog-article__subtitle">{safe_subtitle}</p><time datetime="{submitted.isoformat()}">{date_text}</time>{image_html}<div class="blog-article__body">{body_html}</div><a class="cta" href="../../#contactos">Falar connosco →</a></main></body></html>'''
    post_path.write_text(page, encoding="utf-8")
    return {"title": title, "subtitle": subtitle, "date": submitted.isoformat(), "url": f"posts/{slug}.html", "image": images[0] if images else ""}


def main():
    BLOG.mkdir(exist_ok=True)
    state = read_state()
    now = datetime.now(timezone.utc)
    last_check = datetime.fromisoformat(state["last_check"]) if state.get("last_check") else None
    force_now = os.getenv("FORCE_NOW", "false").lower() == "true"
    if last_check and now - last_check < INTERVAL and not force_now:
        print("Ainda não passaram 48 horas desde a última verificação.")
        return
    if force_now:
        print("Execução manual: a verificar respostas novas pendentes agora.")
    info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    credentials = Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly", "https://www.googleapis.com/auth/drive.readonly"]
    )
    session = AuthorizedSession(credentials)
    _, rows = sheets_rows(session)
    processed = set(state.get("processed", []))
    if not state.get("last_check"):
        # First run establishes a baseline so old form responses are not published unexpectedly.
        state["processed"] = [row_id(row) for row in rows]
        state["last_check"] = now.isoformat()
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Ponto inicial guardado; {len(rows)} respostas antigas não serão publicadas.")
        return
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    manifest = json.loads(POSTS_PATH.read_text(encoding="utf-8")) if POSTS_PATH.exists() else []
    slugs = {Path(p["url"]).stem for p in manifest}
    published_count = 0
    for row in rows:
        identifier = row_id(row)
        if identifier in processed:
            continue
        post = publish_post(row, session, client, slugs)
        if post:
            manifest.insert(0, post)
            published_count += 1
        processed.add(identifier)
    state["processed"] = sorted(processed)
    state["last_check"] = now.isoformat()
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    POSTS_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Verificação concluída; publicados {published_count} artigos.")


if __name__ == "__main__":
    main()
