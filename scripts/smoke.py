#!/usr/bin/env python3
"""Basic DOCX conversion and LightRAG lifecycle smoke; --index adds model retrieval."""
import io
import json
import sys
import subprocess
import time
import urllib.error
import urllib.request
import uuid
import zipfile

from runtime import Runtime, parser


class SmokeError(RuntimeError):
    """A diagnostic containing only test-owned metadata."""


OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def request(method, url, headers=None, body=None, timeout=300):
    headers = dict(headers or {})
    if isinstance(body, dict):
        body = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with OPENER.open(req, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise SmokeError(f"HTTP {exc.code} during {method} request") from None


def docx(marker):
    text = (
        "Oriole warehouse receiving policy. "
        f"Every inbound shipment must use receiving code {marker}. "
        "The quality team quarantines damaged cartons for exactly 37 hours. "
        "The warehouse supervisor records the receiving code before releasing the shipment."
    )
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            '</Types>')
        archive.writestr("_rels/.rels",
            '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
            '</Relationships>')
        archive.writestr("word/document.xml",
            '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f'<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>')
    return out.getvalue()


def convert(url, content):
    boundary = "smoke-" + uuid.uuid4().hex
    body = bytearray()
    for key, value in {"to_formats": "md", "do_ocr": "true", "force_ocr": "false"}.items():
        body.extend(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n".encode())
    body.extend(f"--{boundary}\r\nContent-Disposition: form-data; name=\"files\"; filename=\"receiving.docx\"\r\nContent-Type: application/vnd.openxmlformats-officedocument.wordprocessingml.document\r\n\r\n".encode())
    body.extend(content)
    body.extend(f"\r\n--{boundary}--\r\n".encode())
    task = request("POST", url + "/v1/convert/file/async",
                   {"Content-Type": f"multipart/form-data; boundary={boundary}"}, bytes(body))
    deadline = time.monotonic() + 600
    while task["task_status"] in ("pending", "started") and time.monotonic() < deadline:
        task = request("GET", url + f"/v1/status/poll/{task['task_id']}?wait=3")
    if task["task_status"] != "success":
        raise SmokeError("Docling did not complete conversion successfully")
    return request("GET", url + f"/v1/result/{task['task_id']}")["document"]["md_content"]


def cleanup(runtime, controller, headers, kb, instance, rag_headers):
    deadline = time.monotonic() + 300
    while True:
        try:
            if instance:
                container = runtime.metadata("container", instance["providerRef"])
                ip = container["NetworkSettings"]["Networks"][runtime.config["NETWORK_NAME"]]["IPAddress"]
                url = f"http://{ip}:9621"
                cleared = request("DELETE", url + "/documents", rag_headers)
                if cleared.get("status") != "success":
                    raise SmokeError("Temporary document cleanup is still busy")
                request("POST", url + "/documents/clear_cache", rag_headers, {})
            request("DELETE", controller + "/v1/instances/" + kb, headers)
            return
        except (SmokeError, subprocess.CalledProcessError):
            if time.monotonic() >= deadline:
                raise
            time.sleep(3)


def main():
    args_parser = parser()
    args_parser.add_argument("--index", action="store_true",
                             help="Also run model/embedding indexing and retrieval")
    args = args_parser.parse_args()
    runtime = Runtime(args.env_file)
    cfg = runtime.config
    controller = "http://127.0.0.1:" + cfg.get("CONTROLLER_PORT", "19632")
    docling = "http://127.0.0.1:" + cfg.get("DOCLING_PORT", "15002")
    headers = {"Authorization": "Bearer " + cfg["LIGHTRAG_CONTROLLER_TOKEN"]}
    rag_headers = {"X-API-Key": cfg["LIGHTRAG_API_KEY"]}
    request("GET", docling + "/health")
    request("GET", controller + "/health", headers)
    print("PASS Docling and Controller health", flush=True)
    suffix = uuid.uuid4().hex
    kb = "kb_deploy_smoke_" + suffix
    marker = "ORIOLE-" + suffix[:12].upper()
    print("Smoke knowledge base: " + kb, flush=True)
    markdown = convert(docling, docx(marker))
    if marker not in markdown:
        raise SmokeError("Markdown did not preserve the source marker")
    print("PASS Docling DOCX conversion", flush=True)
    instance = None
    try:
        instance = request("PUT", controller + "/v1/instances/" + kb, headers, {})
        container = runtime.metadata("container", instance["providerRef"])
        ip = container["NetworkSettings"]["Networks"][cfg["NETWORK_NAME"]]["IPAddress"]
        url = f"http://{ip}:9621"
        health = request("GET", url + "/health", rag_headers)
        if instance.get("status") != "ready" or health.get("status") not in ("healthy", "ok"):
            raise SmokeError("LightRAG instance is not healthy")
        if (health.get("configuration") or {}).get("workspace") != instance["workspace"]:
            raise SmokeError("LightRAG workspace identity does not match")
        print("PASS LightRAG startup, database connection and workspace identity", flush=True)
        if args.index:
            source = kb + "-receiving.docx"
            inserted = request("POST", url + "/documents/text", rag_headers,
                               {"text": markdown, "file_source": source})
            deadline = time.monotonic() + 600
            while True:
                state = request("GET", url + "/documents/track_status/" + inserted["track_id"], rag_headers)
                states = [item.get("status") for item in state.get("documents", [])]
                if states and all(value == "processed" for value in states):
                    break
                if "failed" in states or time.monotonic() >= deadline:
                    raise SmokeError("Indexing failed or timed out")
                time.sleep(3)
            print("PASS real model/embedding indexing", flush=True)
            result = request("POST", url + "/query/data", rag_headers,
                             {"query": "What receiving code and quarantine interval does Oriole warehouse use?",
                              "mode": "hybrid", "top_k": 5, "chunk_top_k": 3,
                              "enable_rerank": False, "include_references": True})
            chunks = (result.get("data") or {}).get("chunks") or []
            if not any(marker in str(chunk.get("content", "")) and chunk.get("file_path") == source
                       for chunk in chunks):
                raise SmokeError("Retrieval did not return the source marker and file")
            print("PASS exact retrieval", flush=True)
    finally:
        cleanup(runtime, controller, headers, kb, instance, rag_headers)
        print("PASS temporary document/cache/instance cleanup", flush=True)
    print("KNOWLEDGE_DEPLOY_SMOKE_OK" if args.index else "KNOWLEDGE_DEPLOY_BASIC_SMOKE_OK")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"SMOKE FAILED: {type(exc).__name__}", file=sys.stderr)
        sys.exit(1)
