"""Desktop WebView2 resource ownership shared by the HUD and console."""
from __future__ import annotations

import logging
import sys


logger = logging.getLogger(__name__)


def disable_unused_response_observer(window):
    """Stop pywebview retaining responses during long-running page polling.

    Neither desktop frontend consumes response_received. WebView2 retains native
    response resources with that observer enabled, even when JS/DOM stays bounded:
    https://github.com/MicrosoftEdge/WebView2Feedback/issues/5138
    """
    if sys.platform != 'win32':
        return
    disabled_browser, disabled_core = None, None

    def before_load():
        try:
            def detach():
                nonlocal disabled_browser, disabled_core
                browser = getattr(window.native, 'browser', None)
                # Other pywebview renderers do not have this WebView2 observer.
                if browser is None or not hasattr(browser, 'on_web_resource_response'):
                    return
                core = browser.webview.CoreWebView2
                if browser is disabled_browser and core == disabled_core:
                    return
                if len(window.events.response_received):
                    logger.warning('应用正在观察网络响应，保留 WebView2 事件；长时间轮询可能继续增长内存')
                    return
                core.WebResourceResponseReceived -= browser.on_web_resource_response
                disabled_browser, disabled_core = browser, core

            if window.native.InvokeRequired:
                from System import Action
                window.native.Invoke(Action(detach))
            else:
                detach()
        except Exception:
            logger.warning('无法停用 WebView2 网络响应观察；长时间轮询可能继续增长内存', exc_info=True)

    window.events.before_load += before_load
