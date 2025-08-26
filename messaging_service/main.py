import asyncio
import logging
import os
import yaml
from pathlib import Path
from typing import Dict, Any
from aiohttp import web

from adapters.telegram.adapter import TelegramAdapter
from plugins.echo_handler import EchoHandler
from plugins.questionnaire_handler import QuestionnaireHandler
from plugins.start_router import StartRouter
from plugins.admin_tools import AdminTools
from plugins.job_board import JobBoard
from plugins.invites import InvitesPlugin
from plugins.product_catalog import ProductCatalog
from plugins.file_router import FileRouter
from plugins.crypto_watcher import CryptoWatcher
from plugins.ticker import TickerPlugin
from plugins.ton_watcher import TonWatcher
from plugins.support import SupportPlugin
from core.service import MessagingService
from dotenv import load_dotenv
from core.event_bus import bus as event_bus
from plugins.menu import MenuPlugin
from plugins.location_demo import LocationDemo
from plugins.dashboard import DashboardPlugin
from plugins.analytics import AnalyticsPlugin
from core import analytics

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

class MessagingApp:
    def __init__(self, config_path: str = "config/config.yaml"):
        self.config = self._load_config(config_path)
        self.services: Dict[str, MessagingService] = {}
        self.plugins: list = []
        # webhook server state
        self._web_runner: web.AppRunner | None = None
        self._web_site: web.TCPSite | None = None

    @staticmethod
    def _load_config(config_path: str) -> Dict[str, Any]:
        """Load and parse the configuration file"""
        base_dir = Path(__file__).parent
        # load .env at repo root and service dir for convenience
        load_dotenv(base_dir / ".env")
        load_dotenv(base_dir.parent / ".env")

        cfg_path_abs = base_dir / config_path
        with open(cfg_path_abs) as f:
            config = yaml.safe_load(f) or {}

        def resolve_env(obj):
            if isinstance(obj, dict):
                return {k: resolve_env(v) for k, v in obj.items()}
            if isinstance(obj, list):
                return [resolve_env(v) for v in obj]
            if isinstance(obj, str):
                s = obj
                # simple ${VAR} replacement
                if s.startswith("${") and s.endswith("}"):
                    var = s[2:-1]
                    return os.getenv(var, "")
                return s
            return obj

        return resolve_env(config)

    def _setup_services(self):
        """Initialize all configured messaging services"""
        # Initialize Telegram service if configured
        if 'telegram' in self.config:
            tg_cfg = self.config['telegram']
            token = tg_cfg.get('token') or tg_cfg.get('telegram_token')
            if token:
                self.services['telegram'] = TelegramAdapter(tg_cfg)
                logger.info("Telegram service initialized")
            else:
                logger.warning("Telegram config present but token missing.")

    def _setup_plugins(self):
        """Initialize all enabled plugins"""
        if 'plugins' not in self.config or 'enabled' not in self.config['plugins']:
            return

        plugin_map = {
            'echo': EchoHandler,
            'questionnaire': QuestionnaireHandler,
            'start_router': StartRouter,
            'admin_tools': AdminTools,
            'job_board': JobBoard,
            'invites': InvitesPlugin,
            'product_catalog': ProductCatalog,
            'crypto_watcher': CryptoWatcher,
            'ton_watcher': TonWatcher,
            'support': SupportPlugin,
            'menu': MenuPlugin,
            'file_router': FileRouter,
            'ticker': TickerPlugin,
            'analytics': AnalyticsPlugin,
        }

        # Load questionnaire flows (single or multiple)
        self.flow_plugins = {}
        plugins_cfg = self.config['plugins']
        # Multiple registry support under plugins.questionnaires
        questionnaires_cfg = plugins_cfg.get('questionnaires') or {}
        for alias, qcfg in questionnaires_cfg.items():
            flow_path = qcfg.get('flow_path')
            if not flow_path:
                logger.warning(f"Questionnaire '{alias}' configured without flow_path")
                continue
            path_abs = (Path(__file__).parent / flow_path).resolve()
            qh = QuestionnaireHandler(str(path_abs), alias=alias)
            self.plugins.append(qh)
            self.flow_plugins[alias] = qh
            logger.info(f"Initialized questionnaire flow: {alias}")

        # Backwards compatibility: single 'questionnaire'
        if 'questionnaire' in plugins_cfg:
            flow_path = plugins_cfg.get('questionnaire', {}).get('flow_path')
            if flow_path:
                path_abs = (Path(__file__).parent / flow_path).resolve()
                qh = QuestionnaireHandler(str(path_abs))
                alias = qh.alias
                self.plugins.append(qh)
                self.flow_plugins[alias] = qh
                logger.info(f"Initialized questionnaire flow: {alias}")
            else:
                logger.warning("Questionnaire plugin enabled but no flow_path configured")

        # Other simple plugins
        for plugin_name in self.config['plugins']['enabled']:
            if plugin_name == 'echo':
                self.plugins.append(EchoHandler())
                logger.info("Initialized plugin: echo")
            elif plugin_name == 'start_router':
                default_flow = self.config.get('start_router', {}).get('default_flow')
                router = StartRouter(flow_registry=self.flow_plugins, default_flow=default_flow)
                self.plugins.append(router)
                logger.info("Initialized plugin: start_router")
            elif plugin_name == 'admin_tools':
                admin_cfg = self.config.get('admin_tools', {})
                self.plugins.append(AdminTools(admin_cfg))
                logger.info("Initialized plugin: admin_tools")
            elif plugin_name == 'job_board':
                jb_cfg = self.config.get('job_board', {})
                admin_cfg = self.config.get('admin_tools', {})
                self.plugins.append(JobBoard(jb_cfg, admin_cfg))
                logger.info("Initialized plugin: job_board")
            elif plugin_name == 'invites':
                inv_cfg = self.config.get('invites', {})
                admin_cfg = self.config.get('admin_tools', {})
                # Gate should be first to intercept when locked
                self.plugins.insert(0, InvitesPlugin(inv_cfg, admin_cfg))
                logger.info("Initialized plugin: invites (registered first)")
            elif plugin_name == 'product_catalog':
                prod_cfg = self.config.get('products', {})
                self.plugins.append(ProductCatalog(prod_cfg))
                logger.info("Initialized plugin: product_catalog")
            elif plugin_name == 'crypto_watcher':
                cw_cfg = self.config  # full tree; plugin reads crypto.usdt
                prod_cfg = self.config.get('products', {})
                self.plugins.append(CryptoWatcher(cw_cfg, prod_cfg))
                logger.info("Initialized plugin: crypto_watcher")
            elif plugin_name == 'ton_watcher':
                tw_cfg = self.config  # full tree; plugin reads crypto.ton and admin_tools
                prod_cfg = self.config.get('products', {})
                self.plugins.append(TonWatcher(tw_cfg, prod_cfg))
                logger.info("Initialized plugin: ton_watcher")
            elif plugin_name == 'menu':
                menus_cfg = self.config.get('menus') or []
                self.plugins.append(MenuPlugin(menus_cfg))
                logger.info("Initialized plugin: menu")
            elif plugin_name == 'file_router':
                fr_cfg = self.config.get('file_router', {})
                self.plugins.append(FileRouter(fr_cfg))
                logger.info("Initialized plugin: file_router")
            elif plugin_name == 'ticker':
                tk_cfg = self.config.get('ticker', {})
                self.plugins.append(TickerPlugin(tk_cfg))
                logger.info("Initialized plugin: ticker")
            elif plugin_name == 'analytics':
                self.plugins.append(AnalyticsPlugin(self.config))
                logger.info("Initialized plugin: analytics")
            elif plugin_name == 'location_demo':
                loc_cfg = self.config.get('location_demo', {})
                self.plugins.append(LocationDemo(loc_cfg))
                logger.info("Initialized plugin: location_demo")
            elif plugin_name == 'dashboard':
                web_cfg = (self.config.get('web_app') or {})
                self.plugins.append(DashboardPlugin(web_cfg))
                logger.info("Initialized plugin: dashboard")
            elif plugin_name == 'support':
                sp_cfg = self.config
                self.plugins.append(SupportPlugin(sp_cfg))
                logger.info("Initialized plugin: support")

    def _register_plugins(self):
        """Register plugins with all services"""
        for service in self.services.values():
            for plugin in self.plugins:
                service.add_handler(plugin)

    async def start(self):
        """Start all services"""
        self._setup_services()
        self._setup_plugins()
        # Initialize analytics DB (best-effort)
        try:
            await analytics.init_db()
        except Exception as e:
            logger.warning(f"Analytics init failed: {e}")
        # Configure event bus from YAML (events.handlers)
        handlers = ((self.config.get('events') or {}).get('handlers')) or []
        try:
            event_bus.register_handlers(handlers)
            # Provide a generic send function using the first available service (telegram preferred)
            async def _send(chat_id: str, text: str) -> None:
                # prefer telegram
                svc = self.services.get('telegram') or next(iter(self.services.values()), None)
                if svc:
                    await svc.send_message(chat_id=str(chat_id), text=text)
            event_bus.set_send_message_func(_send)
            logger.info("Event bus configured with %d handler(s)", len(handlers))
        except Exception as e:
            logger.warning(f"Failed to configure event bus: {e}")
        self._register_plugins()

        logger.info("Starting messaging services...")
        for name, service in self.services.items():
            try:
                await service.start()
                logger.info(f"Started {name} service")
            except Exception as e:
                logger.error(f"Failed to start {name} service: {e}")

        # Start webhook server if configured
        try:
            await self._maybe_start_webhook()
        except Exception as e:
            logger.error(f"Failed to start webhook server: {e}")

    async def stop(self):
        """Stop all services gracefully"""
        logger.info("Stopping messaging services...")
        for name, service in self.services.items():
            try:
                await service.stop()
                logger.info(f"Stopped {name} service")
            except Exception as e:
                logger.error(f"Error stopping {name} service: {e}")
        # Stop webhook server
        try:
            if self._web_site:
                await self._web_site.stop()
            if self._web_runner:
                await self._web_runner.cleanup()
        except Exception as e:
            logger.warning(f"Error stopping webhook server: {e}")

    # ---- Webhook server ----
    async def _maybe_start_webhook(self) -> None:
        wh = self.config.get('webhook') or {}
        enabled = bool(wh) and str(wh.get('enabled', 'true')).lower() != 'false'
        if not enabled:
            return
        host = wh.get('host') or '0.0.0.0'
        port = int(wh.get('port') or 8081)
        path = wh.get('path') or '/webhook/payment'
        secret = wh.get('secret') or ''

        async def handle_payment(request: web.Request) -> web.Response:
            try:
                # simple secret validation from header or query
                provided = request.headers.get('x-webhook-secret') or request.query.get('secret') or ''
                if secret and provided != secret:
                    return web.json_response({"error": "unauthorized"}, status=401)
                data = await request.json()
                # expected minimal payload
                user_id = str(data.get('user_id') or '')
                chat_id = str(data.get('chat_id') or user_id or '')
                product_id = str(data.get('product_id') or '')
                token = str(data.get('token') or '')
                amount = data.get('amount')
                currency = data.get('currency')
                meta = data.get('meta') or {}
                payload = {
                    'user_id': user_id,
                    'chat_id': chat_id,
                    'product_id': product_id,
                    'token': token,
                    'amount': amount,
                    'currency': currency,
                    'meta': meta,
                    'provider': 'external',
                }
                # emit event for observability/integration
                await event_bus.emit('payment.external.confirmed', payload)
                # message user with deep-link to trigger delivery flow in ProductCatalog
                try:
                    svc = self.services.get('telegram') or next(iter(self.services.values()), None)
                    if svc and chat_id and token:
                        msg = f"Payment received for {product_id or 'your order'}. Tap to finalize: /start deliver-{token}"
                        await svc.send_message(chat_id=chat_id, text=msg)
                except Exception as e:
                    logger.warning(f"Failed to notify user about payment: {e}")
                return web.json_response({"ok": True})
            except Exception as e:
                logger.error(f"Webhook error: {e}")
                return web.json_response({"ok": False, "error": str(e)}, status=400)

        app = web.Application()
        app.router.add_post(path, handle_payment)
        # Minimal WebApp static route
        async def web_app_page(request: web.Request) -> web.Response:
            html = """
<!DOCTYPE html>
<html>
  <head>
    <meta charset=\"utf-8\" />
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
    <title>Telegram WebApp Demo</title>
    <script src=\"https://telegram.org/js/telegram-web-app.js\"></script>
    <style>body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu,\"Helvetica Neue\",Arial,sans-serif;padding:16px;background:#111;color:#eee}</style>
  </head>
  <body>
    <h2>WebApp Demo</h2>
    <div id=\"info\">Loading user info...</div>
    <script>
      const tg = window.Telegram?.WebApp; 
      if (tg) {
        tg.ready();
        const init = tg.initDataUnsafe || {};
        document.getElementById('info').textContent = 'Hello ' + (init?.user?.username || 'guest') + ' (id ' + (init?.user?.id || '?') + ')';
        tg.expand();
      } else {
        document.getElementById('info').textContent = 'Not in Telegram WebApp context';
      }
    </script>
  </body>
</html>
"""
            return web.Response(text=html, content_type='text/html')
        app.router.add_get('/webapp', web_app_page)
        self._web_runner = web.AppRunner(app)
        await self._web_runner.setup()
        self._web_site = web.TCPSite(self._web_runner, host=host, port=port)
        await self._web_site.start()
        logger.info(f"Webhook server running on http://{host}:{port}{path}")

async def main():
    app = MessagingApp()
    
    try:
        await app.start()
        # Keep the application running
        while True:
            await asyncio.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutting down...")
        await app.stop()
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        await app.stop()
        raise

if __name__ == "__main__":
    asyncio.run(main())
