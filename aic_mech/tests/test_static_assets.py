"""静态前端契约测试。

前端只有两个入口页面：接诊室 (reception.html) 与电脑工作站 (workstation.html)，
共用 scene.css（样式单一来源）、scene.js（交互层）、audio_alerts.js（监护音）。

旧版三栏工作台 (index.html / app.js / ecg_canvas.js / dag_view.js / zoom_adapter.js /
custom.css) 已整体移除。这里保留一条反悔护栏：被删除的入口不得以任何形式回流成死引用。
"""
import os
import re
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(PROJECT_ROOT, "static")

PAGES = ("reception.html", "workstation.html")
SCENE_CSS = os.path.join(STATIC_DIR, "css", "scene.css")
SCENE_JS = os.path.join(STATIC_DIR, "js", "scene.js")
AUDIO_JS = os.path.join(STATIC_DIR, "js", "audio_alerts.js")

# 已删除的旧版工作台资源：若重新出现在 static/ 或被页面引用，说明死代码被带回来了
LEGACY_ASSETS = (
    "index.html",
    "css/custom.css",
    "js/app.js",
    "js/ecg_canvas.js",
    "js/dag_view.js",
    "js/zoom_adapter.js",
)


def _slurp(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def _page(name):
    return _slurp(os.path.join(STATIC_DIR, name))


class TestStaticAssetsConsistency(unittest.TestCase):
    """双页前端：资源存在性、引用可达性、样式与交互层契约。"""

    def test_static_files_existence(self):
        """双页及其共享资源必须存在。"""
        for name in PAGES:
            path = os.path.join(STATIC_DIR, name)
            self.assertTrue(os.path.isfile(path), f"缺少页面: {path}")
        for path in (SCENE_CSS, SCENE_JS, AUDIO_JS):
            self.assertTrue(os.path.isfile(path), f"缺少共享资源: {path}")

    def test_legacy_workbench_removed(self):
        """旧版工作台已移除，不得重新出现在 static/ 或任一页面的引用中。"""
        for rel in LEGACY_ASSETS:
            self.assertFalse(
                os.path.exists(os.path.join(STATIC_DIR, rel)),
                f"旧版工作台资源不应存在: static/{rel}",
            )
        for name in PAGES:
            html = _page(name)
            for rel in LEGACY_ASSETS:
                self.assertNotIn(rel, html, f"{name} 仍引用已删除的 {rel}")

    def test_pages_load_shared_scene_layer(self):
        """两页必须加载同一套 scene.css / scene.js / audio_alerts.js，且不再内联样式。"""
        for name in PAGES:
            html = _page(name)
            self.assertIn("/static/css/scene.css", html, f"{name} 未加载 scene.css")
            self.assertIn("/static/js/scene.js", html, f"{name} 未加载 scene.js")
            self.assertIn("/static/js/audio_alerts.js", html, f"{name} 未加载 audio_alerts.js")
            self.assertNotIn(
                "<style", html, f"{name} 出现内联 <style>，样式必须留在 scene.css"
            )

    def test_every_static_reference_resolves(self):
        """页面引用的每个 /static/ 资源都必须真实存在，防止删文件后留下 404 死链。"""
        for name in PAGES:
            refs = re.findall(r'(?:href|src)="(/static/[^"?#]+)', _page(name))
            self.assertTrue(refs, f"{name} 未引用任何 /static/ 资源")
            for ref in refs:
                target = os.path.join(
                    STATIC_DIR, ref[len("/static/"):].replace("/", os.sep)
                )
                self.assertTrue(os.path.isfile(target), f"{name} 引用了不存在的资源: {ref}")

    def test_scene_class_names_have_css_rules(self):
        """scene.js 动态挂载的 scene-* 元素必须在 scene.css 中有对应规则。"""
        code = _slurp(SCENE_JS)
        css = _slurp(SCENE_CSS)
        names = set(re.findall(r"""["'](scene-[a-z0-9-]+)["']""", code))
        self.assertTrue(names, "scene.js 中未找到 scene-* 类名")
        missing = sorted(n for n in names if f".{n}" not in css)
        self.assertEqual(missing, [], f"scene.js 使用但 scene.css 缺规则: {missing}")

    def test_scene_waveform_renderer(self):
        """监护波形由 scene.js 生成 SVG polyline 点集，工作站须保留波形容器。"""
        code = _slurp(SCENE_JS)
        for token in (
            "function ecgY",
            "renderDynamicWaveform",
            "setAttribute('points'",
            "waveLines",
        ):
            self.assertIn(token, code, f"scene.js 缺少波形渲染要素: {token}")
        workstation = _page("workstation.html")
        self.assertIn('class="wave-svg"', workstation, "工作站缺少 wave-svg 波形容器")
        self.assertIn("<polyline", workstation, "工作站缺少 polyline 波形线")

    def test_audio_alerts_synthesizer_config(self):
        """Web Audio 合成器常量与降级保护。"""
        code = _slurp(AUDIO_JS)
        self.assertIn("AudioContext", code)
        self.assertIn("900", code)  # 高优先级报警频率 (IEC 60601-1-8)
        self.assertIn("playQRSBeep", code)
        self.assertIn("playCriticalAlarm", code)

    def test_emotion_panel_present_on_both_pages(self):
        """两页都必须有侧边状态面板：三条 [data-channel]（信任/患者焦虑/家属焦虑）。"""
        for name in PAGES:
            html = _page(name)
            self.assertIn('id="emotion-panel"', html, f"{name} 缺少 #emotion-panel")
            self.assertIn('class="scene-emotion-panel"', html, f"{name} 缺少 scene-emotion-panel")
            for channel in ("trust", "patient_anxiety", "family_anxiety"):
                self.assertIn(
                    f'data-channel="{channel}"', html, f"{name} 缺少 data-channel={channel}"
                )
            self.assertEqual(
                len(re.findall(r'data-channel="', html)), 3, f"{name} 的 [data-channel] 行数应为 3"
            )

    def test_emotion_css_token_layer(self):
        """:root 必须提供一组 --scene-* 令牌，供新样式引用。"""
        css = _slurp(SCENE_CSS)
        root = re.search(r":root\{(.*?)\}", css, re.S)
        self.assertIsNotNone(root, "scene.css 缺少 :root 令牌层")
        block = root.group(1)
        for token in (
            "--scene-trust",
            "--scene-anxiety",
            "--scene-family",
            "--scene-safe",
            "--scene-warn",
            "--scene-danger",
            "--scene-mute",
            "--scene-track",
            "--scene-bar-dur",
            "--scene-panel-bg",
            "--scene-panel-border",
            "--scene-panel-ink",
        ):
            self.assertIn(f"{token}:", block, f":root 缺少 CSS 变量 {token}")
        self.assertGreaterEqual(
            css.count("var(--scene-"), 10, "新样式未引用 --scene-* 变量"
        )

    def test_emotion_classes_have_css_rules(self):
        """状态栏 / 危险区 / 拒绝配合 / 结束态 / 免责声明的类名必须在 scene.css 有规则。"""
        css = _slurp(SCENE_CSS)
        code = _slurp(SCENE_JS)
        for name in (
            "renderEmotionPanel",
            "startEmotionTicker",
            "ensureDisclaimer",
            "ackDisclaimer",
            "terminal_reason",
            "state.ended",
        ):
            self.assertIn(name, code, f"scene.js 缺少 {name}")
        for cls in (
            "scene-emotion-panel",
            "scene-emotion-row",
            "scene-emotion-label",
            "scene-emotion-bar",
            "scene-emotion-fill",
            "scene-emotion-value",
            "scene-emotion-trend",
            "scene-band-low",
            "scene-band-high",
            "scene-band-critical",
            "scene-trend-shown",
            "scene-trend-good",
            "scene-trend-bad",
            "scene-refuse",
            "scene-refusing",
            "scene-refusal-banner",
            "scene-ended",
            "scene-disclaimer-modal",
            "scene-disclaimer-ack",
        ):
            self.assertIn(f".{cls}", css, f"scene.css 缺少 .{cls} 规则")

    def test_page_scene_class_names_have_css_rules(self):
        """页面里静态写出的 scene-* 类名也必须在 scene.css 有规则（骨架不得无样式）。"""
        css = _slurp(SCENE_CSS)
        missing = set()
        for name in PAGES:
            for attribute in re.findall(r'class="([^"]*)"', _page(name)):
                for cls in attribute.split():
                    if cls.startswith("scene-") and f".{cls}" not in css:
                        missing.add(cls)
        self.assertEqual(sorted(missing), [], f"页面使用但 scene.css 缺规则: {sorted(missing)}")

    def test_pages_have_no_inline_style(self):
        """页面禁止内联 style=""，布局必须来自 scene.css。"""
        for name in PAGES:
            html = _page(name)
            self.assertNotIn('style="', html, f"{name} 仍有内联 style 属性")

    def test_no_death_countdown_text(self):
        """死亡倒计时文案必须移除；rupture_countdown 只服务声光报警。"""
        for label, text in (
            ("scene.js", _slurp(SCENE_JS)),
            ("reception.html", _page("reception.html")),
            ("workstation.html", _page("workstation.html")),
        ):
            self.assertNotIn("倒计时", text, f"{label} 仍出现「倒计时」字样")
        self.assertIn("rupture_countdown", _slurp(SCENE_JS), "报警仍需 rupture_countdown")

    def test_disclaimer_storage_key_and_copy(self):
        """免责声明首次进入弹一次，使用 sessionStorage 键与规格原文。"""
        code = _slurp(SCENE_JS)
        self.assertIn("clinmind-disclaimer-ack", code, "scene.js 缺少免责声明 sessionStorage 键")
        self.assertIn("sessionStorage", code, "scene.js 未使用 sessionStorage")
        self.assertIn("scene-disclaimer-modal", code, "scene.js 未接管免责声明弹窗")
        for name in PAGES:
            html = _page(name)
            self.assertIn('class="scene-dialog scene-disclaimer-modal"', html, f"{name} 缺少免责声明弹窗")
            self.assertIn("本系统为医学教育模拟工具", html, f"{name} 缺少免责声明原文")
            self.assertIn("不构成任何诊疗建议", html, f"{name} 免责声明原文不完整")


if __name__ == "__main__":
    unittest.main(verbosity=2)
