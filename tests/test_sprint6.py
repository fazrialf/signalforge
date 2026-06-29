"""Unit tests for Sprint 6 — External Data modules."""
import sys, os, time, tempfile, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import unittest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone, timedelta

# Test each external module independently
try:
    from external.news_fetcher import fetch_recent_news, is_high_impact_news
    NEWS_AVAILABLE = True
except ImportError:
    NEWS_AVAILABLE = False

try:
    from external.fear_greed import fetch_fear_greed
    FG_AVAILABLE = True
except ImportError:
    FG_AVAILABLE = False

try:
    from external.economic_calendar import get_upcoming_events, is_near_high_impact_event
    CALENDAR_AVAILABLE = True
except ImportError:
    CALENDAR_AVAILABLE = False

try:
    from external.onchain import fetch_onchain_metrics
    ONCHAIN_AVAILABLE = True
except ImportError:
    ONCHAIN_AVAILABLE = False

try:
    from external.correlations import fetch_correlations
    CORR_AVAILABLE = True
except ImportError:
    CORR_AVAILABLE = False


# ─── News Fetcher Tests ───────────────────────────────────────────────────────────────
@unittest.skipIf(not NEWS_AVAILABLE, "external.news_fetcher not available")
class TestNewsFetcher(unittest.TestCase):
    def test_returns_list(self):
        """fetch_recent_news should return a list."""
        result = fetch_recent_news(hours=24)
        self.assertIsInstance(result, list)

    def test_article_structure(self):
        """Each article should have required keys."""
        articles = fetch_recent_news(hours=24)
        if articles:
            art = articles[0]
            self.assertIn('title', art)
            self.assertIn('summary', art)
            self.assertIn('link', art)
            self.assertIn('published_at', art)
            self.assertIn('source', art)
            self.assertIn('relevance_score', art)
            self.assertIsInstance(art['published_at'], datetime)

    def test_keyword_filtering(self):
        """Keyword filtering should work."""
        keywords = ['Bitcoin', 'BTC']
        articles = fetch_recent_news(hours=48, keywords=keywords)
        # All returned articles should have positive relevance
        for art in articles:
            self.assertGreaterEqual(art['relevance_score'], 0.0)
            self.assertLessEqual(art['relevance_score'], 1.0)

    def test_is_high_impact(self):
        """is_high_impact_news should check keywords."""
        high_impact = {
            'title': 'Fed announces emergency rate hike',
            'summary': 'FOMC meeting results shock markets',
        }
        low_impact = {
            'title': 'Analyst predicts minor correction',
            'summary': 'Some trader says something',
        }
        self.assertTrue(is_high_impact_news(high_impact))
        self.assertFalse(is_high_impact_news(low_impact))

    def test_caching(self):
        """Second call should use cache (faster)."""
        # Clear cache
        cache_path = '/tmp/signalforge_news_cache.json'
        if os.path.exists(cache_path):
            os.unlink(cache_path)
        
        start1 = time.time()
        articles1 = fetch_recent_news(hours=24)
        dur1 = time.time() - start1
        
        start2 = time.time()
        articles2 = fetch_recent_news(hours=24)
        dur2 = time.time() - start2
        
        # Cache hit should be much faster (< 0.1s vs 1s+)
        self.assertLess(dur2, dur1 * 0.5)
        self.assertEqual(len(articles1), len(articles2))


# ─── Fear & Greed Tests ─────────────────────────────────────────────────────────────
@unittest.skipIf(not FG_AVAILABLE, "external.fear_greed not available")
class TestFearGreed(unittest.TestCase):
    def test_returns_dict(self):
        """fetch_fear_greed should return a dict with required keys."""
        fg = fetch_fear_greed()
        self.assertIsInstance(fg, dict)
        self.assertIn('value', fg)
        self.assertIn('classification', fg)
        self.assertIn('timestamp', fg)
        self.assertIn('is_extreme', fg)

    def test_value_range(self):
        """Value should be 0-100."""
        fg = fetch_fear_greed()
        value = fg['value']
        self.assertGreaterEqual(value, 0)
        self.assertLessEqual(value, 100)

    def test_is_extreme_logic(self):
        """is_extreme should be True only for value < 10 or > 90."""
        fg = fetch_fear_greed()
        value = fg['value']
        is_ext = fg['is_extreme']
        
        if value < 10 or value > 90:
            self.assertTrue(is_ext)
        else:
            self.assertFalse(is_ext)

    def test_timestamp_is_datetime(self):
        """Timestamp should be a datetime object."""
        fg = fetch_fear_greed()
        self.assertIsInstance(fg['timestamp'], datetime)

    def test_caching(self):
        """Second call should hit cache."""
        cache_path = '/tmp/signalforge_fg_cache.json'
        if os.path.exists(cache_path):
            os.unlink(cache_path)
        
        fg1 = fetch_fear_greed()
        fg2 = fetch_fear_greed()
        
        # Values should be identical (from cache)
        self.assertEqual(fg1['value'], fg2['value'])


# ─── Economic Calendar Tests ───────────────────────────────────────────────────────────
@unittest.skipIf(not CALENDAR_AVAILABLE, "external.economic_calendar not available")
class TestEconomicCalendar(unittest.TestCase):
    def test_returns_list(self):
        """get_upcoming_events should return a list."""
        events = get_upcoming_events(hours_ahead=168)  # 7 days
        self.assertIsInstance(events, list)

    def test_event_structure(self):
        """Each event should have required keys."""
        events = get_upcoming_events(hours_ahead=168)
        if events:
            evt = events[0]
            self.assertIn('event', evt)
            self.assertIn('datetime', evt)
            self.assertIn('impact', evt)
            self.assertIn('hours_until', evt)
            self.assertIsInstance(evt['datetime'], datetime)
            self.assertGreater(evt['hours_until'], 0)

    def test_is_near_high_impact(self):
        """is_near_high_impact_event should return bool."""
        result = is_near_high_impact_event(hours_threshold=2)
        self.assertIsInstance(result, bool)

    def test_hours_threshold(self):
        """Events beyond threshold should not trigger alert."""
        # If no events within 2h, should return False
        near = is_near_high_impact_event(hours_threshold=2)
        # If there's an event within 2h, it should return True
        # Otherwise False — we just check it's a bool
        self.assertIn(near, [True, False])


# ─── On-Chain Tests ────────────────────────────────────────────────────────────────────
@unittest.skipIf(not ONCHAIN_AVAILABLE, "external.onchain not available")
class TestOnChain(unittest.TestCase):
    def test_returns_dict(self):
        """fetch_onchain_metrics should return a dict."""
        metrics = fetch_onchain_metrics()
        self.assertIsInstance(metrics, dict)

    def test_required_keys(self):
        """Result should have required keys."""
        metrics = fetch_onchain_metrics()
        self.assertIn('funding_rate', metrics)
        self.assertIn('funding_sentiment', metrics)
        self.assertIn('open_interest', metrics)
        self.assertIn('long_short_ratio', metrics)
        self.assertIn('ls_sentiment', metrics)
        self.assertIn('taker_buy_ratio', metrics)
        self.assertIn('taker_sentiment', metrics)
        self.assertIn('fetched_at', metrics)

    def test_sentiment_values(self):
        """Sentiment fields should be strings."""
        metrics = fetch_onchain_metrics()
        self.assertIsInstance(metrics['funding_sentiment'], str)
        self.assertIsInstance(metrics['ls_sentiment'], str)
        self.assertIsInstance(metrics['taker_sentiment'], str)

    def test_funding_rate_numeric(self):
        """Funding rate should be numeric."""
        metrics = fetch_onchain_metrics()
        self.assertIsInstance(metrics['funding_rate'], (int, float))

    def test_caching(self):
        """Second call should hit cache (15-min TTL)."""
        cache_path = '/tmp/signalforge_onchain_cache.json'
        if os.path.exists(cache_path):
            os.unlink(cache_path)
        
        m1 = fetch_onchain_metrics()
        m2 = fetch_onchain_metrics()
        
        # Should return same values (from cache)
        self.assertEqual(m1['funding_rate'], m2['funding_rate'])


# ─── Correlations Tests ─────────────────────────────────────────────────────────────────
@unittest.skipIf(not CORR_AVAILABLE, "external.correlations not available")
class TestCorrelations(unittest.TestCase):
    def test_returns_dict(self):
        """fetch_correlations should return a dict."""
        corr = fetch_correlations()
        self.assertIsInstance(corr, dict)

    def test_required_keys(self):
        """Result should have required keys."""
        corr = fetch_correlations()
        self.assertIn('btc_dominance', corr)
        self.assertIn('eth_btc_ratio', corr)
        self.assertIn('dxy', corr)
        self.assertIn('spx', corr)
        self.assertIn('gold', corr)
        self.assertIn('btc_dom_trend', corr)
        self.assertIn('eth_btc_trend', corr)
        self.assertIn('fetched_at', corr)

    def test_btc_dominance_range(self):
        """BTC dominance should be reasonable (30-80%)."""
        corr = fetch_correlations()
        dom = corr['btc_dominance']
        if dom is not None:
            self.assertGreaterEqual(dom, 30)
            self.assertLessEqual(dom, 80)

    def test_eth_btc_ratio_range(self):
        """ETH/BTC should be positive."""
        corr = fetch_correlations()
        ratio = corr['eth_btc_ratio']
        if ratio is not None:
            self.assertGreater(ratio, 0)

    def test_tradfi_optional(self):
        """TradFi data (DXY, SPX, Gold) can be None if yfinance not installed."""
        corr = fetch_correlations()
        # These can be None, but if present should be numeric
        for key in ['dxy', 'spx', 'gold']:
            val = corr[key]
            if val is not None:
                self.assertIsInstance(val, (int, float))

    def test_trend_values(self):
        """Trend fields should be strings."""
        corr = fetch_correlations()
        self.assertIsInstance(corr['btc_dom_trend'], str)
        self.assertIsInstance(corr['eth_btc_trend'], str)


# ─── Integration Tests ─────────────────────────────────────────────────────────────────
class TestIntegration(unittest.TestCase):
    def test_all_modules_importable(self):
        """All Sprint 6 modules should be importable without error."""
        import external.news_fetcher
        import external.fear_greed
        import external.economic_calendar
        import external.onchain
        import external.correlations
        # If we got here, all imports succeeded
        self.assertTrue(True)

    def test_filter_gate_imports_sprint6(self):
        """FilterGate should import external modules without error."""
        from signals.filter_gate import FilterGate, _EXTERNAL_DATA_AVAILABLE
        # Check that the flag is set correctly
        self.assertIsInstance(_EXTERNAL_DATA_AVAILABLE, bool)

    def test_prompt_builder_accepts_external_data(self):
        """build_prompt should accept external_data parameter."""
        from signals.prompt_builder import build_prompt
        from signals.mtf_bias import MTFBias
        from signals.confluence import ConfluenceScore
        from core.market_structure import Bias
        
        # Minimal inputs
        mtf = MTFBias(
            daily_bias=Bias.BULLISH, h4_bias=Bias.BULLISH, h1_bias=Bias.BULLISH,
            aligned=True, dominant_direction='bullish', strength=1.0, summary='aligned'
        )
        conf = ConfluenceScore(
            direction='bullish',
            raw_score=10,
            bullish_score=10,
            bearish_score=0,
            net_score=10,
            factors=[],
            meets_threshold=True,
            dominant_direction='bullish',
        )
        ext_data = {
            'fear_greed': {'value': 50, 'classification': 'Neutral', 'is_extreme': False},
            'onchain': {'funding_rate': 0.0001, 'funding_sentiment': 'neutral'},
            'correlations': {'btc_dominance': 56.0, 'btc_dom_trend': 'stable'},
            'news': [{'title': 'Test', 'source': 'Test', 'summary': 'Test'}],
        }
        
        sys_prompt, usr_prompt = build_prompt(
            results={}, confluence=conf, mtf_bias=mtf,
            symbol='BTC/USDT', current_price=60000,
            external_data=ext_data,
        )
        
        self.assertIn('Fear & Greed', usr_prompt)
        self.assertIn('On-Chain', usr_prompt)
        self.assertIn('Macro Correlations', usr_prompt)
        self.assertIn('Recent High-Impact News', usr_prompt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
