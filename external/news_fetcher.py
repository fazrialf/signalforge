"""RSS-based crypto news scraper for CoinDesk and CoinTelegraph."""

import feedparser
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# RSS feed URLs
COINDESK_RSS = "https://www.coindesk.com/arc/outboundfeeds/rss/"
COINTELEGRAPH_RSS = "https://cointelegraph.com/rss"

# Cache settings
CACHE_FILE = Path("/tmp/signalforge_news_cache.json")
CACHE_TTL_SECONDS = 600  # 10 minutes

# High-impact keywords for news classification
HIGH_IMPACT_KEYWORDS = [
    "fed", "fomc", "cpi", "nfp", "sec", "etf", "regulation",
    "binance", "coinbase", "hack", "exploit", "federal reserve",
    "interest rate", "inflation", "treasury", "lawsuit", "ban"
]


def _parse_rss_date(date_str: str) -> Optional[datetime]:
    """Parse RSS pubDate string to datetime object in UTC.
    
    Args:
        date_str: Date string from RSS feed (RFC 822 format).
    
    Returns:
        datetime object in UTC, or None if parsing fails.
    """
    try:
        # feedparser returns time.struct_time in published_parsed
        # We'll use that instead of manually parsing date strings
        return None
    except Exception as e:
        logger.warning(f"Failed to parse date '{date_str}': {e}")
        return None


def _calculate_relevance_score(text: str, keywords: list[str]) -> float:
    """Calculate relevance score based on keyword matches.
    
    Args:
        text: Text to search (title + summary).
        keywords: List of keywords to match.
    
    Returns:
        Score between 0 and 1 (matches / len(keywords)).
    """
    if not keywords:
        return 1.0
    
    text_lower = text.lower()
    matches = sum(1 for kw in keywords if kw.lower() in text_lower)
    return matches / len(keywords)


def _load_cache() -> Optional[dict]:
    """Load cached news data if valid.
    
    Returns:
        Cached data dict or None if cache is invalid/expired.
    """
    try:
        if not CACHE_FILE.exists():
            return None
        
        with open(CACHE_FILE, 'r') as f:
            cache = json.load(f)
        
        # Check if cache is still valid
        cache_time = cache.get('timestamp', 0)
        if time.time() - cache_time > CACHE_TTL_SECONDS:
            return None
        
        return cache
    except Exception as e:
        logger.warning(f"Failed to load cache: {e}")
        return None


def _save_cache(data: list[dict]) -> None:
    """Save news data to cache.
    
    Args:
        data: List of article dicts to cache.
    """
    try:
        cache = {
            'timestamp': time.time(),
            'articles': data
        }
        with open(CACHE_FILE, 'w') as f:
            json.dump(cache, f)
    except Exception as e:
        logger.warning(f"Failed to save cache: {e}")


def _fetch_feed(url: str, source_name: str) -> list[dict]:
    """Fetch and parse a single RSS feed.
    
    Args:
        url: RSS feed URL.
        source_name: Name of the source (for metadata).
    
    Returns:
        List of article dicts.
    """
    articles = []
    
    try:
        feed = feedparser.parse(url)
        
        if feed.bozo and not feed.entries:
            logger.error(f"Failed to parse {source_name} feed: {feed.bozo_exception}")
            return []
        
        for entry in feed.entries:
            try:
                # Parse publication date
                published_at = None
                if hasattr(entry, 'published_parsed') and entry.published_parsed:
                    published_at = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
                elif hasattr(entry, 'updated_parsed') and entry.updated_parsed:
                    published_at = datetime(*entry.updated_parsed[:6], tzinfo=timezone.utc)
                
                if not published_at:
                    logger.debug(f"Skipping article without date: {entry.get('title', 'Unknown')}")
                    continue
                
                # Extract article data
                article = {
                    'title': entry.get('title', '').strip(),
                    'summary': entry.get('summary', entry.get('description', '')).strip(),
                    'link': entry.get('link', '').strip(),
                    'published_at': published_at,
                    'source': source_name,
                    'relevance_score': 0.0  # Will be calculated later
                }
                
                articles.append(article)
                
            except Exception as e:
                logger.warning(f"Failed to parse entry from {source_name}: {e}")
                continue
        
        logger.info(f"Fetched {len(articles)} articles from {source_name}")
        
    except Exception as e:
        logger.error(f"Failed to fetch {source_name} feed: {e}")
    
    return articles


def fetch_recent_news(hours: int = 24, keywords: Optional[list[str]] = None) -> list[dict]:
    """Fetch crypto news from CoinDesk + CoinTelegraph RSS feeds.
    
    Args:
        hours: Look back N hours from now.
        keywords: Filter for specific keywords (e.g., ['Bitcoin', 'BTC', 'Fed', 'regulation']).
                  If None, return all recent articles.
    
    Returns:
        List of dicts:
        [
            {
                "title": str,
                "summary": str,  # excerpt/description
                "link": str,
                "published_at": datetime,  # UTC
                "source": str,  # 'CoinDesk' or 'CoinTelegraph'
                "relevance_score": float,  # 0-1 based on keyword match
            },
            ...
        ]
        Sorted by published_at DESC, limited to 20 most recent.
    """
    # Try loading from cache first
    cache = _load_cache()
    if cache:
        logger.info("Using cached news data")
        all_articles = cache['articles']
        # Convert ISO strings back to datetime objects
        for article in all_articles:
            article['published_at'] = datetime.fromisoformat(article['published_at'])
    else:
        # Fetch from RSS feeds
        logger.info("Fetching fresh news from RSS feeds")
        all_articles = []
        
        # Fetch CoinDesk
        all_articles.extend(_fetch_feed(COINDESK_RSS, 'CoinDesk'))
        
        # Fetch CoinTelegraph
        all_articles.extend(_fetch_feed(COINTELEGRAPH_RSS, 'CoinTelegraph'))
        
        if not all_articles:
            logger.warning("No articles fetched from any source")
            return []
        
        # Convert datetime to ISO strings for caching
        cache_articles = []
        for article in all_articles:
            cache_article = article.copy()
            cache_article['published_at'] = article['published_at'].isoformat()
            cache_articles.append(cache_article)
        
        _save_cache(cache_articles)
    
    # Filter by time window
    cutoff_time = datetime.now(timezone.utc) - timedelta(hours=hours)
    recent_articles = [
        article for article in all_articles
        if article['published_at'] >= cutoff_time
    ]
    
    logger.info(f"Found {len(recent_articles)} articles within last {hours} hours")
    
    # Calculate relevance scores and filter by keywords
    filtered_articles = []
    for article in recent_articles:
        text = f"{article['title']} {article['summary']}"
        relevance_score = _calculate_relevance_score(text, keywords or [])
        article['relevance_score'] = relevance_score
        
        # If keywords specified, only include articles with matches
        if keywords is None or relevance_score > 0:
            filtered_articles.append(article)
    
    # Sort by published_at DESC and limit to 20
    filtered_articles.sort(key=lambda x: x['published_at'], reverse=True)
    result = filtered_articles[:20]
    
    logger.info(f"Returning {len(result)} articles after filtering")
    return result


def is_high_impact_news(article: dict) -> bool:
    """Returns True if article likely affects BTC price short-term.
    
    Keywords: Fed, FOMC, CPI, NFP, SEC, ETF, regulation, Binance, Coinbase, hack, exploit.
    
    Args:
        article: Article dict with 'title' and 'summary' keys.
    
    Returns:
        True if article contains high-impact keywords.
    """
    text = f"{article.get('title', '')} {article.get('summary', '')}".lower()
    return any(keyword in text for keyword in HIGH_IMPACT_KEYWORDS)


def get_recent_articles(max_age_minutes: int = 1440, keywords: Optional[list[str]] = None) -> list[dict]:
    """Thin adapter over fetch_recent_news() using a minutes-based window.

    Args:
        max_age_minutes: Look back this many minutes from now.
        keywords: Optional keyword filter list passed through to fetch_recent_news.

    Returns:
        List of article dicts, same schema as fetch_recent_news().
    """
    hours = max_age_minutes / 60.0
    return fetch_recent_news(hours=hours, keywords=keywords)


def get_news_sentiment(symbol: str | None = None, max_age_minutes: int = 1440) -> str:
    """Derive an overall sentiment string from recent high-impact news.

    Returns 'bullish', 'bearish', or 'neutral'.
    Looks at articles published in the last max_age_minutes.
    If symbol is given (e.g. 'BTC'), also match symbol-specific articles.
    """
    try:
        keywords = [symbol.replace("/USDT", "").replace("/", "").lower()] if symbol else None
        articles = get_recent_articles(max_age_minutes=max_age_minutes, keywords=keywords)
        if not articles:
            # Fallback: check general crypto news for high-impact items
            articles = get_recent_articles(max_age_minutes=max_age_minutes)

        bullish_keywords = [
            "rally", "surge", "breakout", "etf approved", "etf inflow",
            "adoption", "upgrade", "partnership", "bullish", "all-time high",
            "ath", "institutional", "buy", "accumulate", "positive"
        ]
        bearish_keywords = [
            "crash", "dump", "ban", "hack", "exploit", "lawsuit", "sec",
            "regulation", "bearish", "sell-off", "liquidation", "fear",
            "inflation", "rate hike", "fomc", "cpi miss", "collapse"
        ]

        bull_score = 0
        bear_score = 0
        for article in articles[:10]:  # only most recent 10
            text = f"{article.get('title', '')} {article.get('summary', '')}".lower()
            weight = 2 if is_high_impact_news(article) else 1
            bull_score += weight * sum(1 for kw in bullish_keywords if kw in text)
            bear_score += weight * sum(1 for kw in bearish_keywords if kw in text)

        if bull_score == 0 and bear_score == 0:
            return "neutral"
        if bull_score > bear_score * 1.5:
            return "bullish"
        if bear_score > bull_score * 1.5:
            return "bearish"
        return "neutral"
    except Exception as e:
        logger.warning("get_news_sentiment error: %s", e)
        return "neutral"
