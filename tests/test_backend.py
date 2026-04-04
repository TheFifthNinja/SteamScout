"""
Tests for Backend.py — covers all pure/deterministic logic without
requiring Steam, WMIC, network, or any OS-specific side effects.
"""

import unittest
import sys
import os

# Ensure the project root is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import Backend


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Size / DirectX parsing helpers
# ═══════════════════════════════════════════════════════════════════════════════

class TestSizeGb(unittest.TestCase):
    """_size_gb converts human-readable storage strings to float GB."""

    def test_gb_plain(self):
        self.assertEqual(Backend._size_gb("8 GB"), 8.0)

    def test_gb_with_decimals(self):
        self.assertAlmostEqual(Backend._size_gb("15.5 GB"), 15.5)

    def test_mb(self):
        self.assertAlmostEqual(Backend._size_gb("512 MB"), 0.5)

    def test_tb(self):
        self.assertEqual(Backend._size_gb("1 TB"), 1024.0)

    def test_default_unit_gb(self):
        # No unit → default GB
        self.assertEqual(Backend._size_gb("16"), 16.0)

    def test_returns_none_for_garbage(self):
        self.assertIsNone(Backend._size_gb("N/A"))
        self.assertIsNone(Backend._size_gb(""))

    def test_mixed_text(self):
        self.assertEqual(Backend._size_gb("Requires 50 GB available space"), 50.0)


class TestDirectxMajor(unittest.TestCase):
    """_directx_major extracts the major DX version number."""

    def test_directx_12(self):
        self.assertEqual(Backend._directx_major("DirectX 12"), 12)

    def test_directx_11(self):
        self.assertEqual(Backend._directx_major("DirectX 11"), 11)

    def test_dx_shorthand(self):
        self.assertEqual(Backend._directx_major("DX 9"), 9)

    def test_version_keyword(self):
        self.assertEqual(Backend._directx_major("Version 11"), 11)

    def test_returns_none_on_garbage(self):
        self.assertIsNone(Backend._directx_major("OpenGL 4.5"))
        self.assertIsNone(Backend._directx_major(""))


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Requirement HTML parsing
# ═══════════════════════════════════════════════════════════════════════════════

class TestParseReqs(unittest.TestCase):
    """_parse_reqs extracts structured data from Steam's HTML requirement blobs."""

    SAMPLE_HTML = (
        '<ul class="bb_ul"><li><strong>OS:</strong> Windows 10 64-bit</li>'
        '<li><strong>Processor:</strong> Intel Core i5-6600K / AMD Ryzen 5 1600</li>'
        '<li><strong>Memory:</strong> 8 GB RAM</li>'
        '<li><strong>Graphics:</strong> NVIDIA GTX 1060 6GB / AMD RX 580</li>'
        '<li><strong>DirectX:</strong> Version 11</li>'
        '<li><strong>Storage:</strong> 50 GB available space</li></ul>'
    )

    def test_extracts_os(self):
        r = Backend._parse_reqs(self.SAMPLE_HTML)
        self.assertIn("os", r)
        self.assertIn("Windows 10", r["os"])

    def test_extracts_cpu(self):
        r = Backend._parse_reqs(self.SAMPLE_HTML)
        self.assertIn("cpu", r)
        self.assertIn("i5-6600K", r["cpu"])

    def test_extracts_ram(self):
        r = Backend._parse_reqs(self.SAMPLE_HTML)
        self.assertIn("ram", r)
        self.assertIn("8", r["ram"])

    def test_extracts_gpu(self):
        r = Backend._parse_reqs(self.SAMPLE_HTML)
        self.assertIn("gpu", r)
        self.assertIn("GTX 1060", r["gpu"])

    def test_extracts_directx(self):
        r = Backend._parse_reqs(self.SAMPLE_HTML)
        self.assertIn("directx", r)
        self.assertIn("11", r["directx"])

    def test_extracts_storage(self):
        r = Backend._parse_reqs(self.SAMPLE_HTML)
        self.assertIn("storage", r)
        self.assertIn("50", r["storage"])

    def test_empty_html_returns_empty(self):
        self.assertEqual(Backend._parse_reqs(""), {})

    def test_partial_html(self):
        html = '<li><strong>Memory:</strong> 16 GB RAM</li>'
        r = Backend._parse_reqs(html)
        self.assertIn("ram", r)
        self.assertNotIn("cpu", r)


# ═══════════════════════════════════════════════════════════════════════════════
# 3. URL classification
# ═══════════════════════════════════════════════════════════════════════════════

class TestClassifyUrl(unittest.TestCase):
    """classify_url should recognise Steam store, library, and internal URLs."""

    def test_store_page(self):
        r = Backend.classify_url("https://store.steampowered.com/app/730/Counter-Strike_2/")
        self.assertIsNotNone(r)
        self.assertEqual(r["app_id"], 730)
        self.assertEqual(r["section"], "Store page")

    def test_store_page_no_trailing(self):
        r = Backend.classify_url("https://store.steampowered.com/app/730")
        self.assertIsNotNone(r)
        self.assertEqual(r["app_id"], 730)

    def test_library_url(self):
        r = Backend.classify_url("steam://nav/games/details/570")
        self.assertIsNotNone(r)
        self.assertEqual(r["app_id"], 570)
        self.assertEqual(r["section"], "Library")

    def test_internal_browser_store(self):
        r = Backend.classify_url("steaminternalbrowser://store/app/440/Team_Fortress_2")
        self.assertIsNotNone(r)
        self.assertEqual(r["app_id"], 440)

    def test_internal_browser_library(self):
        r = Backend.classify_url("steaminternalbrowser://library/app/1091500")
        self.assertIsNotNone(r)
        self.assertEqual(r["app_id"], 1091500)
        self.assertEqual(r["section"], "Library")

    def test_steam_protocol_openurl(self):
        r = Backend.classify_url("steam://openurl/https://store.steampowered.com/app/292030/The_Witcher_3")
        self.assertIsNotNone(r)
        self.assertEqual(r["app_id"], 292030)

    def test_store_dlc_section(self):
        r = Backend.classify_url("https://store.steampowered.com/app/730/dlc")
        self.assertIsNotNone(r)
        self.assertEqual(r["section"], "DLC")

    def test_store_reviews_section(self):
        r = Backend.classify_url("https://store.steampowered.com/app/730/reviews")
        self.assertIsNotNone(r)
        self.assertEqual(r["section"], "Reviews")

    def test_non_steam_url_returns_none(self):
        self.assertIsNone(Backend.classify_url("https://www.google.com"))

    def test_tiny_appid_rejected(self):
        # AppID < 10 is considered invalid
        self.assertIsNone(Backend.classify_url("https://store.steampowered.com/app/5/Foo"))

    def test_empty_url(self):
        self.assertIsNone(Backend.classify_url(""))


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Hardware name normalisation
# ═══════════════════════════════════════════════════════════════════════════════

class TestNormaliseHwName(unittest.TestCase):
    """_normalise_hw_name strips vendor prefixes and collapses whitespace."""

    def test_nvidia_prefix(self):
        self.assertEqual(Backend._normalise_hw_name("NVIDIA GeForce RTX 4070"), "rtx 4070")

    def test_amd_radeon_prefix(self):
        self.assertEqual(Backend._normalise_hw_name("AMD Radeon RX 7900 XTX"), "rx 7900 xtx")

    def test_intel_core_prefix(self):
        self.assertEqual(Backend._normalise_hw_name("Intel Core i7-12700K"), "i7-12700k")

    def test_whitespace_collapsing(self):
        self.assertEqual(Backend._normalise_hw_name("  NVIDIA   GeForce   RTX   3060  "), "rtx 3060")

    def test_empty_string(self):
        self.assertEqual(Backend._normalise_hw_name(""), "")

    def test_none_input(self):
        self.assertEqual(Backend._normalise_hw_name(None), "")


# ═══════════════════════════════════════════════════════════════════════════════
# 5. GPU benchmark lookup & scoring
# ═══════════════════════════════════════════════════════════════════════════════

class TestGpuLookup(unittest.TestCase):
    """_fuzzy_gpu_lookup and _gpu_score should resolve GPU names to scores."""

    def test_exact_match(self):
        self.assertEqual(Backend._fuzzy_gpu_lookup("RTX 4070"), 26919)

    def test_vendor_prefix_stripped(self):
        self.assertEqual(Backend._fuzzy_gpu_lookup("NVIDIA GeForce RTX 3060"), 17005)

    def test_suffix_stripped(self):
        self.assertEqual(Backend._fuzzy_gpu_lookup("GTX 1060 6GB"), 10049)

    def test_model_variant_suffix(self):
        # "OC" / "Gaming" suffixes should be stripped for matching
        self.assertEqual(Backend._fuzzy_gpu_lookup("RTX 4060 Ti OC"), 22614)

    def test_unknown_gpu_returns_none(self):
        self.assertIsNone(Backend._fuzzy_gpu_lookup("SomeFakeGPU 9999"))

    def test_gpu_score_table_hit(self):
        score = Backend._gpu_score("NVIDIA GeForce GTX 1080 Ti")
        self.assertEqual(score, 18591.0)

    def test_gpu_score_heuristic_fallback(self):
        # A GPU not in the table should still return something from the regex fallback
        score = Backend._gpu_score("GTX 1035")
        self.assertIsNotNone(score)
        self.assertGreater(score, 0)

    def test_gpu_score_none_for_garbage(self):
        self.assertIsNone(Backend._gpu_score("Potato"))

    def test_amd_gpu_lookup(self):
        self.assertEqual(Backend._fuzzy_gpu_lookup("RX 7900 XTX"), 31407)

    def test_intel_arc_lookup(self):
        self.assertEqual(Backend._fuzzy_gpu_lookup("Arc A770"), 17200)

    def test_laptop_mx_series(self):
        self.assertEqual(Backend._fuzzy_gpu_lookup("MX450"), 4200)


class TestGpuScoreOrdering(unittest.TestCase):
    """Sanity-check that GPU scores follow expected performance hierarchy."""

    def test_rtx_4090_beats_3090(self):
        self.assertGreater(Backend._gpu_score("RTX 4090"), Backend._gpu_score("RTX 3090"))

    def test_rtx_3070_beats_2070(self):
        self.assertGreater(Backend._gpu_score("RTX 3070"), Backend._gpu_score("RTX 2070"))

    def test_gtx_1080_beats_1060(self):
        self.assertGreater(Backend._gpu_score("GTX 1080"), Backend._gpu_score("GTX 1060"))

    def test_rx_7900_xtx_beats_6900_xt(self):
        self.assertGreater(Backend._gpu_score("RX 7900 XTX"), Backend._gpu_score("RX 6900 XT"))

    def test_gtx_1060_beats_750_ti(self):
        self.assertGreater(Backend._gpu_score("GTX 1060"), Backend._gpu_score("GTX 750 Ti"))


# ═══════════════════════════════════════════════════════════════════════════════
# 6. CPU benchmark lookup & scoring
# ═══════════════════════════════════════════════════════════════════════════════

class TestCpuLookup(unittest.TestCase):
    """_fuzzy_cpu_lookup and _cpu_score should resolve CPU names to scores."""

    def test_exact_match(self):
        self.assertEqual(Backend._fuzzy_cpu_lookup("i7-12700K"), 17500)

    def test_vendor_prefix(self):
        self.assertEqual(Backend._fuzzy_cpu_lookup("Intel Core i5-12400"), 13000)

    def test_intel_without_dash(self):
        # "i7 12700K" (space instead of dash) should still resolve
        self.assertEqual(Backend._fuzzy_cpu_lookup("i7 12700K"), 17500)

    def test_ryzen_lookup(self):
        self.assertEqual(Backend._fuzzy_cpu_lookup("AMD Ryzen 5 5600X"), 14500)

    def test_substring_match(self):
        # Full CPU string containing a known model
        score = Backend._fuzzy_cpu_lookup("AMD Ryzen 7 5800X 8-Core Processor")
        self.assertEqual(score, 16000)

    def test_unknown_returns_none(self):
        self.assertIsNone(Backend._fuzzy_cpu_lookup("FakeCPU 2000"))

    def test_cpu_score_table_hit(self):
        score = Backend._cpu_score("Intel Core i9-13900K")
        self.assertEqual(score, 20000.0)

    def test_cpu_score_heuristic_fallback(self):
        # A model not in table should use regex fallback
        score = Backend._cpu_score("Intel Core i5-15400")
        self.assertIsNotNone(score)
        self.assertGreater(score, 0)

    def test_cpu_score_none_for_garbage(self):
        self.assertIsNone(Backend._cpu_score("Potato"))

    def test_fx_series(self):
        self.assertEqual(Backend._fuzzy_cpu_lookup("FX-8350"), 4800)


class TestCpuScoreOrdering(unittest.TestCase):
    """Sanity-check CPU score ordering matches real-world performance."""

    def test_13900k_beats_10400(self):
        self.assertGreater(Backend._cpu_score("i9-13900K"), Backend._cpu_score("i5-10400"))

    def test_5600x_beats_3600(self):
        self.assertGreater(Backend._cpu_score("Ryzen 5 5600X"), Backend._cpu_score("Ryzen 5 3600"))

    def test_12700k_beats_8700k(self):
        self.assertGreater(Backend._cpu_score("i7-12700K"), Backend._cpu_score("i7-8700K"))

    def test_ryzen_7_5800x_beats_2700x(self):
        self.assertGreater(Backend._cpu_score("Ryzen 7 5800X"), Backend._cpu_score("Ryzen 7 2700X"))


# ═══════════════════════════════════════════════════════════════════════════════
# 7. _required_score (handles "or" / "/" alternatives)
# ═══════════════════════════════════════════════════════════════════════════════

class TestRequiredScore(unittest.TestCase):
    """_required_score should parse Steam requirement text with alternatives."""

    def test_single_gpu(self):
        val = Backend._required_score("NVIDIA GTX 1060", "gpu")
        self.assertIsNotNone(val)
        self.assertEqual(val, 10049.0)

    def test_or_alternatives_takes_min(self):
        val = Backend._required_score("NVIDIA GTX 1060 / AMD RX 580", "gpu")
        self.assertIsNotNone(val)
        # Should be the lower of 10049 and 8791
        self.assertEqual(val, 8791.0)

    def test_cpu_alternatives(self):
        val = Backend._required_score("Intel Core i5-6600K or AMD Ryzen 5 1600", "cpu")
        self.assertIsNotNone(val)
        # i5-6600K=7800, Ryzen 5 1600=7000 → min is 7000
        self.assertEqual(val, 7000.0)

    def test_unparseable_returns_none(self):
        self.assertIsNone(Backend._required_score("any compatible GPU", "gpu"))


# ═══════════════════════════════════════════════════════════════════════════════
# 8. _ratio helper
# ═══════════════════════════════════════════════════════════════════════════════

class TestRatio(unittest.TestCase):
    """_ratio computes capped measured/required ratios."""

    def test_exact_match(self):
        self.assertEqual(Backend._ratio(10.0, 10.0), 1.0)

    def test_double(self):
        self.assertEqual(Backend._ratio(20.0, 10.0), 2.0)

    def test_capped_at_3(self):
        self.assertEqual(Backend._ratio(100.0, 10.0), 3.0)

    def test_custom_cap(self):
        self.assertEqual(Backend._ratio(100.0, 10.0, cap=5.0), 5.0)

    def test_zero_required(self):
        self.assertIsNone(Backend._ratio(10.0, 0.0))

    def test_none_measured(self):
        self.assertIsNone(Backend._ratio(None, 10.0))

    def test_none_required(self):
        self.assertIsNone(Backend._ratio(10.0, None))

    def test_below_one(self):
        self.assertAlmostEqual(Backend._ratio(5.0, 10.0), 0.5)

    def test_floor_zero(self):
        # Negative measured shouldn't cause negative ratio
        self.assertEqual(Backend._ratio(-5.0, 10.0), 0.0)


# ═══════════════════════════════════════════════════════════════════════════════
# 9. _score_to_band
# ═══════════════════════════════════════════════════════════════════════════════

class TestScoreToBand(unittest.TestCase):
    """_score_to_band converts score + bottleneck to FPS band tuples."""

    def test_very_low_score(self):
        low, high, note = Backend._score_to_band(0.3, 0.3)
        self.assertLessEqual(high, 20)
        self.assertIn("unplayable", note.lower())

    def test_meets_requirements(self):
        low, high, note = Backend._score_to_band(1.05, 1.0)
        self.assertGreaterEqual(low, 30)

    def test_overkill(self):
        low, high, note = Backend._score_to_band(2.5, 2.5)
        self.assertGreaterEqual(low, 90)
        self.assertIn("overkill", note.lower())

    def test_bottleneck_drags_down(self):
        # High score but very low bottleneck should lower the band
        _, high_balanced, _ = Backend._score_to_band(1.5, 1.5)
        _, high_bottlenecked, _ = Backend._score_to_band(1.5, 0.5)
        self.assertGreater(high_balanced, high_bottlenecked)

    def test_returns_three_element_tuple(self):
        result = Backend._score_to_band(1.0, 1.0)
        self.assertEqual(len(result), 3)
        self.assertIsInstance(result[0], int)
        self.assertIsInstance(result[1], int)
        self.assertIsInstance(result[2], str)


# ═══════════════════════════════════════════════════════════════════════════════
# 10. estimate_performance (simple fallback)
# ═══════════════════════════════════════════════════════════════════════════════

class TestEstimatePerformance(unittest.TestCase):
    """estimate_performance provides preset FPS estimates from pass/fail verdicts."""

    def test_below_minimum(self):
        r = Backend.estimate_performance({"overall_min": "fail", "overall_rec": "fail"})
        self.assertIn("presets", r)
        self.assertIn("Not playable", r["presets"]["high"])

    def test_meets_min_not_rec(self):
        r = Backend.estimate_performance({"overall_min": "pass", "overall_rec": "fail"})
        self.assertIn("35-60", r["presets"]["low"])

    def test_meets_both(self):
        r = Backend.estimate_performance({"overall_min": "pass", "overall_rec": "pass"})
        self.assertIn("60+", r["presets"]["low"])

    def test_unknown(self):
        r = Backend.estimate_performance({"overall_min": "unknown", "overall_rec": "unknown"})
        self.assertEqual(r["presets"]["low"], "Unknown")


# ═══════════════════════════════════════════════════════════════════════════════
# 11. check_compatibility (integration-level with mock data)
# ═══════════════════════════════════════════════════════════════════════════════

class TestCheckCompatibility(unittest.TestCase):
    """check_compatibility with synthetic PC specs and requirements."""

    PC_GOOD = {
        "cpu": "Intel Core i7-12700K",
        "gpu": "NVIDIA GeForce RTX 3070",
        "ram_gb": 32.0,
        "disk_free_gb": 200.0,
        "vram_gb": 8.0,
        "os": "Windows 11 Home",
        "directx": "DirectX 12",
    }

    REQS_TYPICAL = {
        "minimum": {
            "os": "Windows 10 64-bit",
            "cpu": "Intel Core i5-6600K",
            "ram": "8 GB",
            "gpu": "NVIDIA GTX 1060",
            "directx": "Version 11",
            "storage": "50 GB",
        },
        "recommended": {
            "os": "Windows 10 64-bit",
            "cpu": "Intel Core i7-9700K",
            "ram": "16 GB",
            "gpu": "NVIDIA RTX 2070",
            "directx": "Version 12",
            "storage": "50 GB",
        },
    }

    def test_good_pc_passes_both(self):
        r = Backend.check_compatibility(self.PC_GOOD, self.REQS_TYPICAL)
        self.assertEqual(r["overall_min"], "pass")
        self.assertEqual(r["overall_rec"], "pass")

    def test_includes_performance(self):
        r = Backend.check_compatibility(self.PC_GOOD, self.REQS_TYPICAL)
        self.assertIn("performance", r)
        perf = r["performance"]
        self.assertIn("presets", perf)
        self.assertIn("low", perf["presets"])

    def test_ram_fail(self):
        pc = dict(self.PC_GOOD, ram_gb=4.0)
        r = Backend.check_compatibility(pc, self.REQS_TYPICAL)
        self.assertEqual(r["minimum"]["ram"]["status"], "fail")

    def test_gpu_fail_for_weak_gpu(self):
        pc = dict(self.PC_GOOD, gpu="NVIDIA GeForce GT 710")
        r = Backend.check_compatibility(pc, self.REQS_TYPICAL)
        self.assertEqual(r["minimum"]["gpu"]["status"], "fail")

    def test_os_pass_win11_for_win10_req(self):
        r = Backend.check_compatibility(self.PC_GOOD, self.REQS_TYPICAL)
        self.assertEqual(r["minimum"]["os"]["status"], "pass")

    def test_directx_fail(self):
        pc = dict(self.PC_GOOD, directx="DirectX 9")
        r = Backend.check_compatibility(pc, self.REQS_TYPICAL)
        self.assertEqual(r["minimum"]["directx"]["status"], "fail")

    def test_storage_fail(self):
        pc = dict(self.PC_GOOD, disk_free_gb=10.0)
        r = Backend.check_compatibility(pc, self.REQS_TYPICAL)
        self.assertEqual(r["minimum"]["storage"]["status"], "fail")

    def test_rec_pass_promotes_minimum(self):
        """If recommended passes for a component, minimum should also pass."""
        # Create a scenario where min might fail but rec passes
        reqs = {
            "minimum": {"gpu": "NVIDIA GTX 1080 Ti"},  # score 18591
            "recommended": {"gpu": "NVIDIA RTX 2060"},  # score 14095
        }
        pc = dict(self.PC_GOOD, gpu="NVIDIA GeForce RTX 2060")
        # RTX 2060 = 14095 < GTX 1080 Ti = 18591 → min fail
        # BUT RTX 2060 = 14095 >= RTX 2060 = 14095 → rec pass
        # Promotion rule should set minimum to pass
        r = Backend.check_compatibility(pc, reqs)
        self.assertEqual(r["recommended"]["gpu"]["status"], "pass")
        self.assertEqual(r["minimum"]["gpu"]["status"], "pass")

    def test_empty_requirements(self):
        r = Backend.check_compatibility(self.PC_GOOD, {"minimum": {}, "recommended": {}})
        self.assertEqual(r["overall_min"], "unavailable")
        self.assertEqual(r["overall_rec"], "unavailable")

    def test_min_only_no_recommended(self):
        """Games with only minimum reqs should get 'unavailable' for recommended."""
        reqs = {
            "minimum": {
                "cpu": "Intel Core i5-6600K",
                "gpu": "NVIDIA GTX 1060",
                "ram": "8 GB",
            },
            "recommended": {},
        }
        r = Backend.check_compatibility(self.PC_GOOD, reqs)
        self.assertEqual(r["overall_min"], "pass")
        self.assertEqual(r["overall_rec"], "unavailable")
        # Performance should still be generated
        self.assertIn("performance", r)
        self.assertIn("presets", r["performance"])

    def test_min_only_missing_recommended_key(self):
        """Games where the recommended key doesn't exist at all."""
        reqs = {
            "minimum": {
                "cpu": "Intel Core i5-6600K",
                "gpu": "NVIDIA GTX 1060",
                "ram": "8 GB",
            },
        }
        r = Backend.check_compatibility(self.PC_GOOD, reqs)
        self.assertEqual(r["overall_min"], "pass")
        self.assertEqual(r["overall_rec"], "unavailable")
        perf = r["performance"]
        self.assertIn("FPS", perf["presets"]["low"])


# ═══════════════════════════════════════════════════════════════════════════════
# 12. ai_predict_performance (integration-level)
# ═══════════════════════════════════════════════════════════════════════════════

class TestAiPredictPerformance(unittest.TestCase):
    """ai_predict_performance returns structured FPS predictions."""

    PC = {
        "cpu": "Intel Core i7-12700K",
        "gpu": "NVIDIA GeForce RTX 3070",
        "ram_gb": 32.0,
        "vram_gb": 8.0,
        "directx": "DirectX 12",
    }

    REQS = {
        "minimum": {
            "cpu": "Intel Core i5-6600K",
            "gpu": "NVIDIA GTX 1060",
            "ram": "8 GB",
            "directx": "Version 11",
        },
        "recommended": {
            "cpu": "Intel Core i7-9700K",
            "gpu": "NVIDIA RTX 2070",
            "ram": "16 GB",
        },
    }

    COMPAT = {"overall_min": "pass", "overall_rec": "pass"}

    def test_returns_presets(self):
        r = Backend.ai_predict_performance(self.PC, self.REQS, self.COMPAT)
        self.assertIn("presets", r)
        for key in ("low", "medium", "high"):
            self.assertIn(key, r["presets"])
            self.assertIn("FPS", r["presets"][key])

    def test_returns_confidence(self):
        r = Backend.ai_predict_performance(self.PC, self.REQS, self.COMPAT)
        self.assertIn(r["confidence"], ("low", "medium", "high"))

    def test_returns_model_name(self):
        r = Backend.ai_predict_performance(self.PC, self.REQS, self.COMPAT)
        self.assertIn("model", r)
        self.assertIn("v3", r["model"])

    def test_returns_bottleneck_label(self):
        r = Backend.ai_predict_performance(self.PC, self.REQS, self.COMPAT)
        self.assertIn(r["bottleneck"], ("GPU", "CPU", "RAM", "VRAM", "Balanced"))

    def test_returns_score(self):
        r = Backend.ai_predict_performance(self.PC, self.REQS, self.COMPAT)
        self.assertIn("score", r)
        self.assertGreater(r["score"], 0)

    def test_returns_metrics(self):
        r = Backend.ai_predict_performance(self.PC, self.REQS, self.COMPAT)
        self.assertIn("metrics", r)
        self.assertIn("one_percent_low", r["metrics"])

    def test_good_pc_gets_decent_fps(self):
        r = Backend.ai_predict_performance(self.PC, self.REQS, self.COMPAT)
        # A 3070 + 12700K vs GTX 1060 + i5-6600K requirements should predict good FPS
        low_preset = r["presets"]["low"]
        # Parse something like "63-104 FPS"
        import re
        m = re.search(r"(\d+)-(\d+)", low_preset)
        self.assertIsNotNone(m, f"Could not parse FPS from: {low_preset}")
        self.assertGreater(int(m.group(2)), 50)

    def test_weak_pc_gets_lower_fps(self):
        weak_pc = {
            "cpu": "Intel Core i3-6100",
            "gpu": "NVIDIA GeForce GTX 750 Ti",
            "ram_gb": 8.0,
            "vram_gb": 2.0,
            "directx": "DirectX 11",
        }
        compat = {"overall_min": "fail", "overall_rec": "fail"}
        r = Backend.ai_predict_performance(weak_pc, self.REQS, compat)
        import re
        m = re.search(r"(\d+)-(\d+)", r["presets"]["low"])
        self.assertIsNotNone(m)
        self.assertLess(int(m.group(2)), 50)

    def test_fallback_when_no_components_scoreable(self):
        # Completely unscorable specs → should fall back to estimate_performance
        weird_pc = {"cpu": "Potato", "gpu": "Banana", "ram_gb": 8.0, "vram_gb": 0.0, "directx": ""}
        reqs = {"minimum": {"cpu": "Magic", "gpu": "Unicorn"}, "recommended": {}}
        r = Backend.ai_predict_performance(weird_pc, reqs, {"overall_min": "unknown"})
        self.assertIn("presets", r)

    def test_min_only_reqs_still_predicts(self):
        """When only minimum reqs exist, predictor should still produce FPS estimates."""
        reqs = {
            "minimum": {
                "cpu": "Intel Core i5-6600K",
                "gpu": "NVIDIA GTX 1060",
                "ram": "8 GB",
                "directx": "Version 11",
            },
            "recommended": {},
        }
        compat = {"overall_min": "pass", "overall_rec": "unavailable"}
        r = Backend.ai_predict_performance(self.PC, reqs, compat)
        self.assertIn("presets", r)
        self.assertIn("FPS", r["presets"]["low"])
        # Note should mention min-only limitation
        self.assertIn("minimum", r.get("note", "").lower())
        # Should still have decent confidence with 3+ scored components
        self.assertIn(r["confidence"], ("low", "medium", "high"))


# ═══════════════════════════════════════════════════════════════════════════════
# 13. Fallback component evaluation
# ═══════════════════════════════════════════════════════════════════════════════

class TestFallbackComponentEval(unittest.TestCase):
    """_fallback_component_eval handles non-standard requirement text."""

    def test_sse3_modern_cpu_passes(self):
        pc = {"cpu": "Intel Core i7-12700K", "gpu": "RTX 3070"}
        r = Backend._fallback_component_eval("cpu", pc, "SSE3 capable processor")
        self.assertIsNotNone(r)
        self.assertEqual(r["status"], "pass")

    def test_pentium4_or_later_modern_passes(self):
        pc = {"cpu": "AMD Ryzen 5 5600X", "gpu": "RX 6600"}
        r = Backend._fallback_component_eval("cpu", pc, "Pentium 4 or later")
        self.assertIsNotNone(r)
        self.assertEqual(r["status"], "pass")

    def test_returns_none_for_normal_requirement(self):
        pc = {"cpu": "Intel Core i5-12400", "gpu": "RTX 3060"}
        r = Backend._fallback_component_eval("cpu", pc, "Intel Core i5-6600K")
        self.assertIsNone(r)


# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    unittest.main()
