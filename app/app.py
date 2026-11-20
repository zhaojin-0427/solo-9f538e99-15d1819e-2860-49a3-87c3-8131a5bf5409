"""口述史馆检索字幕 —— Flask 网页入口。"""
from __future__ import annotations

import io
import os
from pathlib import Path

from flask import (
    Flask,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename

from . import core

DEFAULT_DB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "oral_history.db")
SCOPES = ("all", "pending")


def create_app(database_path: str | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(
        DATABASE=os.environ.get("ORAL_HISTORY_DB")
        or database_path
        or DEFAULT_DB,
        MAX_CONTENT_LENGTH=32 * 1024 * 1024,
        SECRET_KEY=os.environ.get("ORAL_HISTORY_SECRET", "oral-history-local"),
        JSON_AS_ASCII=False,
    )
    Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)
    core.init_db(app.config["DATABASE"])
    app.teardown_appcontext(close_db)

    @app.template_filter("ts")
    def _ts(value: int) -> str:
        return core.format_timestamp(value)

    @app.errorhandler(RequestEntityTooLarge)
    def _too_large(_exc):
        flash(
            "本次提交超过 32 MiB 上限，服务器没有保存任何内容。"
            "请回到草稿页按页校订，每页最多 20 个片段。",
            "error",
        )
        return redirect(url_for("index"))

    @app.get("/")
    def index():
        conn = get_db()
        draft = core.active_draft(conn)
        draft_info = None
        if draft is not None:
            # 旧卷首次进入工作台时完成一次性回填；之后不再重算。
            core.ensure_persisted(conn, draft["id"])
            stats = core.draft_statistics(conn, draft["id"])
            draft_info = {
                "id": draft["id"],
                "revision": draft["revision"],
                "vtt_filename": draft["vtt_filename"],
                "csv_filename": draft["csv_filename"],
                "created_at": draft["created_at"],
                **stats,
            }
        return render_template(
            "index.html", draft=draft_info, editions=core.list_editions(conn)
        )

    @app.post("/upload")
    def upload():
        vtt_file = request.files.get("vtt")
        csv_file = request.files.get("csv")
        if vtt_file is None or not vtt_file.filename:
            flash("请选择单期原片的 WebVTT 文件。", "error")
            return redirect(url_for("index"))
        if csv_file is None or not csv_file.filename:
            flash("请选择剪辑 CSV 文件。", "error")
            return redirect(url_for("index"))
        try:
            vtt_raw = vtt_file.read().decode("utf-8-sig")
        except UnicodeDecodeError:
            flash("WebVTT 文件不是 UTF-8 编码。", "error")
            return redirect(url_for("index"))
        csv_bytes = csv_file.read()
        try:
            csv_raw = csv_bytes.decode("utf-8-sig")
        except UnicodeDecodeError:
            # 中文表格软件导出的 CSV 常为 GBK/GB18030。
            try:
                csv_raw = csv_bytes.decode("gb18030")
            except UnicodeDecodeError:
                flash("剪辑 CSV 不是 UTF-8 或 GB18030 编码。", "error")
                return redirect(url_for("index"))

        try:
            cues = core.parse_vtt(vtt_raw)
            cuts = core.parse_cuts_csv(csv_raw)
        except core.ParseError as exc:
            flash(f"上传失败：{exc}", "error")
            return redirect(url_for("index"))

        conn = get_db()
        draft_id = core.create_draft(
            conn,
            vtt_raw,
            cuts,
            cues,
            vtt_filename=secure_filename(vtt_file.filename) or vtt_file.filename,
            csv_filename=secure_filename(csv_file.filename) or csv_file.filename,
        )
        flash("已建立草稿：剪辑表中每一次出现都已重新计算节选时码。", "ok")
        return redirect(url_for("draft_page", draft_id=draft_id))

    @app.get("/draft/<int:draft_id>")
    def draft_page(draft_id: int):
        conn = get_db()
        if core.load_draft(conn, draft_id) is None:
            abort(404)
        # 旧草稿首次载入：此处一次性回填，整份草稿生命周期仅此一次重算。
        core.ensure_persisted(conn, draft_id)
        page, scope = _page_params()
        view = _draft_view(conn, draft_id, page, scope)
        return render_template("draft.html", **view), (409 if view["stale"] else 200)

    @app.post("/draft/<int:draft_id>/save")
    def save(draft_id: int):
        return _submit(draft_id, publish=False)

    @app.post("/draft/<int:draft_id>/publish")
    def publish(draft_id: int):
        return _submit(draft_id, publish=True)

    @app.get("/editions")
    def editions():
        conn = get_db()
        return render_template("editions.html", editions=core.list_editions(conn))

    @app.get("/editions/<int:version_no>")
    def edition_detail(version_no: int):
        conn = get_db()
        edition = core.get_edition(conn, version_no)
        if edition is None:
            abort(404)
        body = edition["vtt_blob"].decode("utf-8")
        return render_template("edition.html", edition=edition, body=body)

    @app.get("/editions/<int:version_no>/download")
    def download(version_no: int):
        conn = get_db()
        edition = core.get_edition(conn, version_no)
        if edition is None:
            abort(404)
        return send_file(
            io.BytesIO(edition["vtt_blob"]),
            mimetype="text/vtt",
            as_attachment=True,
            download_name=f"oral-history-v{version_no}.vtt",
            max_age=0,
        )

    return app


# ---------------------------------------------------------------------------
# 请求辅助
# ---------------------------------------------------------------------------


def get_db():
    from flask import current_app

    if "db" not in g:
        g.db = core.connect(current_app.config["DATABASE"])
    return g.db


def close_db(_exc=None) -> None:
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def _page_params() -> tuple[int, str]:
    try:
        page = int(request.args.get("page", "1"))
    except (TypeError, ValueError):
        page = 1
    scope = request.args.get("scope", "all")
    if scope not in SCOPES:
        scope = "all"
    return max(1, page), scope


def _form_revision() -> int:
    try:
        return int(request.form.get("revision", ""))
    except (TypeError, ValueError):
        abort(400, "缺少校订版本号")


def _form_page() -> tuple[int, str]:
    try:
        page = int(request.form.get("page", "1"))
    except (TypeError, ValueError):
        page = 1
    scope = request.form.get("scope", "all")
    if scope not in SCOPES:
        scope = "all"
    return max(1, page), scope


def _collect_changes(form) -> list[dict]:
    """只收集本次表单显示的出现（按持久化 occurrence id）：id:<n> / text:<n>。"""
    changes: list[dict] = []
    for key in form.keys():
        if not key.startswith("status:"):
            continue
        occurrence_id = key[len("status:") :]
        try:
            occurrence_id = int(occurrence_id)
        except ValueError:
            continue
        changes.append(
            {
                "occurrence_id": occurrence_id,
                "status": form.get(key),
                "text": form.get(f"text:{occurrence_id}", ""),
            }
        )
    changes.sort(key=lambda item: item["occurrence_id"])
    return changes


def _draft_view(conn, draft_id: int, page: int, scope: str) -> dict:
    bundle = core.load_page(conn, draft_id, page, scope)
    if bundle is None:
        abort(404)
    row = bundle["row"]
    decisions = bundle["decisions"]
    inherited = bundle["inherited"]
    stats = bundle["stats"]

    cards = []
    for fragment in bundle["fragments"]:
        frag_cards = []
        for occ in fragment.occurrences:
            decision = decisions.get(occ.signature)
            frag_cards.append(
                {
                    "occ": occ,
                    "decision": decision,
                    "inherited": occ.kind == core.CUT and occ.signature in inherited,
                    "pending": core.is_pending(occ, decisions),
                }
            )
        cards.append({"fragment": fragment, "occurrences": frag_cards})

    return {
        "row": row,
        "cards": cards,
        "stats": stats,
        "pending_count": stats["pending_count"],
        "fragment_count": stats["fragment_count"],
        "occurrence_count": stats["occurrence_count"],
        "cut_count": stats["cut_count"],
        "total_ms": stats["total_ms"],
        "page": bundle["page"],
        "pages": bundle["pages"],
        "page_size": bundle["page_size"],
        "scope": bundle["scope"],
        "scope_total": bundle["scope_total"],
        "stale": bundle["stale"],
        "stale_message": (
            "校订页已过期：剪辑表已更换或该页已提交过，请刷新后重新录入。"
            if bundle["stale"]
            else None
        ),
        "error": None,
    }


def _stale_response(conn, draft_id: int, page: int, scope: str):
    view = _draft_view(conn, draft_id, page, scope)
    view["stale"] = True
    view["stale_message"] = "校订页已过期：剪辑表已更换或该页已提交过，请刷新后重新录入。"
    return render_template("draft.html", **view), 409


def _submit(draft_id: int, *, publish: bool):
    conn = get_db()
    row = core.load_draft(conn, draft_id)
    if row is None:
        abort(404)
    revision = _form_revision()
    page, scope = _form_page()
    changes = _collect_changes(request.form)
    try:
        if publish:
            # 页面上的编辑与发布一次提交：先按同一版本号保存本页，再发布。
            if changes:
                core.save_page(conn, draft_id, revision, changes)
            version_no = core.publish(conn, draft_id, revision)
        else:
            core.save_page(conn, draft_id, revision, changes)
    except core.StaleDraft:
        return _stale_response(conn, draft_id, page, scope)
    except core.ValidationError as exc:
        flash(str(exc), "error")
        return redirect(
            url_for("draft_page", draft_id=draft_id, page=page, scope=scope)
        )
    except core.PublishBlocked as exc:
        view = _draft_view(conn, draft_id, page, scope)
        view["error"] = (
            f"还有 {exc.count} 处剪口字幕未处理，处理完才能发布。"
            "已处理的内容已保留。"
        )
        return render_template("draft.html", **view), 422

    if publish:
        flash(f"已发布第 {version_no} 版。", "ok")
        return redirect(url_for("edition_detail", version_no=version_no))
    flash("本页校订已保存。", "ok")
    return redirect(url_for("draft_page", draft_id=draft_id, page=page, scope=scope))
