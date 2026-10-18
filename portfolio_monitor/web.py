import json
import os
import secrets
import time
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .auth import verify_password
from .config import Config
from .db import Repository
from .market import Market, provider_status
from .service import collect, seed

ROOT = Path(__file__).parent


def create_app(config=None, repo=None, market=None):
    config = config or Config.load()
    config.validate()
    state = {"repo": repo, "market": market, "error": None}

    @asynccontextmanager
    async def lifespan(app):
        if not state["market"]:
            state["market"] = Market(config.demo)
        try:
            if not state["repo"]:
                state["repo"] = Repository(config)
            seed(state["repo"])
            if config.demo and not state["repo"].reports():
                collect(state["repo"], state["market"])
        except Exception:
            state["repo"] = None
            state["error"] = "云端数据库尚未配置或暂不可用。请检查 Turso 连接及服务状态。"
        yield
        state["market"].close()

    app = FastAPI(title="Portfolio Monitor", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(SessionMiddleware, secret_key=config.secret or secrets.token_urlsafe(32),
                       https_only=config.production, same_site="strict", max_age=43200)
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
    templates = Jinja2Templates(directory=ROOT / "templates")
    attempts = {}

    def repository():
        if state["repo"] is None:
            raise HTTPException(503, "云端数据库不可用，请检查配置")
        return state["repo"]

    def csrf(request, token):
        expected = request.session.get("csrf", "")
        if not expected or not secrets.compare_digest(expected, token):
            raise HTTPException(403, "表单已过期，请刷新页面")

    def render(request, template, **context):
        if "csrf" not in request.session:
            request.session["csrf"] = secrets.token_urlsafe(32)
        status_code = context.pop("status", 200)
        return templates.TemplateResponse(request=request, name=template, status_code=status_code, context={
            "demo": config.demo, "csrf": request.session["csrf"],
            "path": request.url.path, "notice": request.query_params.get("notice", ""),
            **context})

    def redirect(path, message=""):
        return RedirectResponse(path + ("?notice=" + quote(message) if message else ""), status_code=303)

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Cache-Control"] = "no-store"
        return response

    # Dependency-free auth middleware is installed inside SessionMiddleware by wrapping routes.
    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return render(request, "error.html", message=exc.detail, status=exc.status_code)

    def require_login(request):
        return bool(request.session.get("authenticated"))

    @app.get("/healthz")
    def health():
        return {"status": "ok" if state["repo"] else "unconfigured", "demo": config.demo}

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        return render(request, "login.html", configured=bool(config.password_hash) or config.demo)

    @app.post("/login")
    def login(request: Request, password: str = Form(""), token: str = Form("")):
        csrf(request, token)
        ip = request.client.host if request.client else "unknown"
        times = [t for t in attempts.get(ip, []) if t > time.monotonic() - 900]
        attempts[ip] = times
        if len(times) >= 10:
            return render(request, "login.html", configured=True, error="尝试过多，请15分钟后再试")
        times.append(time.monotonic())
        valid = verify_password(password, config.password_hash)
        if config.demo and not config.password_hash:
            valid = True
        if not valid:
            return render(request, "login.html", configured=bool(config.password_hash), error="密码不正确或尚未配置")
        request.session.clear()
        request.session["authenticated"] = True
        request.session["csrf"] = secrets.token_urlsafe(32)
        attempts.pop(ip, None)
        return redirect("/")

    @app.post("/logout")
    def logout(request: Request, token: str = Form("")):
        csrf(request, token)
        request.session.clear()
        return redirect("/login")

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        if not require_login(request):
            return redirect("/login")
        if state["repo"] is None:
            return render(request, "error.html", message=state["error"], status=503)
        r = repository()
        reports = r.reports()
        return render(request, "dashboard.html", report=reports[0] if reports else None,
                      watch_count=len(r.watchlist()), reports_count=len(reports),
                      settings=r.settings(), cache_warning=r.cache_warning)

    @app.get("/watchlist", response_class=HTMLResponse)
    def watchlist(request: Request, q: str = ""):
        if not require_login(request):
            return redirect("/login")
        r = repository()
        candidates, error = [], None
        if q:
            try:
                candidates = state["market"].search(q)[:5]
            except Exception:
                error = "搜索失败或尚未配置行情密钥；请检查数据源设置。"
        # Bound candidate metadata to keep the signed session within cookie limits.
        request.session["candidates"] = {p["id"]: p for p in candidates}
        return render(request, "watchlist.html", products=[p for p in r.products() if p["id"] in r.watchlist()],
                      candidates=candidates, query=q, error=error)

    @app.post("/watchlist/add")
    def add(request: Request, product_id: str = Form(), token: str = Form()):
        if not require_login(request):
            return redirect("/login")
        csrf(request, token)
        p = request.session.get("candidates", {}).get(product_id)
        if not p:
            raise HTTPException(400, "请先搜索并确认产品")
        repository().add_product(p, watch=True)
        return redirect("/watchlist", "已添加，点击更新行情生成日报")

    @app.post("/watchlist/remove")
    def remove(request: Request, product_id: str = Form(), token: str = Form()):
        if not require_login(request):
            return redirect("/login")
        csrf(request, token)
        repository().unwatch(product_id)
        return redirect("/watchlist", "已移除自选，历史行情仍保留")

    @app.post("/collect")
    def generate(request: Request, token: str = Form()):
        if not require_login(request):
            return redirect("/login")
        csrf(request, token)
        try:
            result = collect(repository(), state["market"])
            message = {"success": "行情已更新；当日日报保留首次生成的快照", "partial": "部分数据更新失败，详情见任务记录",
                       "failed": "行情均未更新成功，未生成新报告", "busy": "已有采集任务正在执行"}.get(result["status"], "任务已完成")
        except Exception:
            message = "云端保存失败，未确认成功。请检查连接后重试。"
        return redirect("/", message)

    @app.get("/product", response_class=HTMLResponse)
    def detail(request: Request, id: str):
        if not require_login(request):
            return redirect("/login")
        r = repository()
        p = next((p for p in r.products() if p["id"] == id), None)
        if not p:
            raise HTTPException(404, "未找到该产品")
        rows = r.history(id, (r.today() - timedelta(days=366)).isoformat(), r.today().isoformat())
        return render(request, "product.html", product=p, rows=rows,
                      chart=json.dumps({"labels": [x["day"] for x in rows], "values": [x["value"] for x in rows]}))

    @app.get("/reports", response_class=HTMLResponse)
    def reports_page(request: Request):
        if not require_login(request):
            return redirect("/login")
        return render(request, "reports.html", reports=repository().reports())

    @app.get("/report/{day}", response_class=HTMLResponse)
    def report_page(request: Request, day: str):
        if not require_login(request):
            return redirect("/login")
        report = repository().report(day)
        if not report:
            raise HTTPException(404, "报告不存在")
        return render(request, "report.html", report=report)

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        if not require_login(request):
            return redirect("/login")
        r = repository()
        used, limit = os.getenv("CLOUD_STORAGE_USED_MB"), os.getenv("CLOUD_STORAGE_LIMIT_MB")
        warning = "云端用量未自动监测，请在 Turso 控制台查看存储、读写额度。"
        try:
            if used and limit and float(limit) > 0:
                percent = float(used) / float(limit) * 100
                warning = f"手动登记的存储用量：{percent:.0f}%" + ("，接近额度，请检查控制台" if percent >= 80 else "")
        except ValueError:
            pass
        return render(request, "settings.html", settings=r.settings(), providers=provider_status(),
                      runs=r.runs(), usage=warning)

    @app.post("/settings")
    def save_settings(request: Request, timezone: str = Form(), report_time: str = Form(),
                      enabled: str = Form(""), token: str = Form()):
        if not require_login(request):
            return redirect("/login")
        csrf(request, token)
        try:
            ZoneInfo(timezone)
            repository().save_settings({"timezone": timezone, "time": report_time, "enabled": enabled == "on"})
        except (ValueError, ZoneInfoNotFoundError):
            raise HTTPException(400, "时区或时间无效") from None
        return redirect("/settings", "设置已保存；云端定时运行还需配置 GitHub Actions")

    @app.get("/api/chart")
    def chart_api(request: Request, id: str):
        if not require_login(request):
            raise HTTPException(401, "请先登录")
        r = repository()
        return r.history(id, (r.today() - timedelta(days=366)).isoformat(), r.today().isoformat())

    return app
