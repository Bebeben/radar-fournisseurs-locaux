"""Enrichissement des contacts (téléphone, email) via site web et recherche Bing.

Pour chaque producteur sans téléphone ou email :
1. Visite le site web du producteur (si disponible)
2. Recherche sur Bing pour trouver le site web et/ou extraire les contacts
3. Scrape le site trouvé pour extraire les contacts
"""

import logging
import re
import time
from urllib.parse import quote_plus, urlparse, unquote

import requests
from bs4 import BeautifulSoup

from config import USER_AGENT, REQUEST_TIMEOUT

logger = logging.getLogger(__name__)

# Domaines à ignorer (plateformes, pas les vrais sites producteurs)
SKIP_DOMAINS = {
    "locavor.fr", "bienvenue-a-la-ferme.com", "facebook.com",
    "instagram.com", "twitter.com", "youtube.com", "linkedin.com",
    "tiktok.com", "google.com", "google.fr", "pagesjaunes.fr",
    "yelp.fr", "tripadvisor.fr", "wikipedia.org", "wikidata.org",
    "bing.com", "microsoft.com",
}


def enrich_contacts(records: list[dict]):
    """Enrichit les contacts manquants via scraping web."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,*/*",
        "Accept-Language": "fr-FR,fr;q=0.9",
    })

    missing = [r for r in records if not r.get("telephone") or not r.get("email")]
    if not missing:
        logger.info("[Contacts] Tous les producteurs ont déjà téléphone et email")
        return

    logger.info(f"[Contacts] {len(missing)} producteurs à enrichir (téléphone/email manquants)")

    enriched = 0
    for i, rec in enumerate(missing):
        nom = rec.get("nom", "")
        ville = rec.get("ville", "")
        site = rec.get("site_web", "")
        needs_phone = not rec.get("telephone")
        needs_email = not rec.get("email")

        if not needs_phone and not needs_email:
            continue

        contacts = {}

        # Étape 1 : scraper le site web du producteur (si c'est un vrai site)
        if site and not _is_platform_url(site):
            contacts = _scrape_website_contacts(session, site)

        # Étape 2 : recherche Bing pour trouver le site et/ou des contacts
        if (not contacts.get("telephone") and needs_phone) or (not contacts.get("email") and needs_email):
            found_url, search_contacts = _search_bing(session, nom, ville)
            for key, val in search_contacts.items():
                if val and not contacts.get(key):
                    contacts[key] = val
            if found_url and _is_platform_url(site):
                rec["site_web"] = found_url

        # Appliquer les contacts trouvés
        applied = False
        if contacts.get("telephone") and needs_phone:
            rec["telephone"] = contacts["telephone"]
            applied = True
        if contacts.get("email") and needs_email:
            rec["email"] = contacts["email"]
            applied = True
        if contacts.get("reseaux_sociaux") and not rec.get("reseaux_sociaux"):
            rec["reseaux_sociaux"] = contacts["reseaux_sociaux"]
        if applied:
            enriched += 1
            logger.debug(f"[Contacts] {nom}: tel={contacts.get('telephone')}, email={contacts.get('email')}")

        if (i + 1) % 10 == 0:
            logger.info(f"[Contacts] {i + 1}/{len(missing)} traités ({enriched} enrichis)")

    logger.info(f"[Contacts] {enriched}/{len(missing)} producteurs enrichis avec contacts")


def _is_platform_url(url: str) -> bool:
    """Vérifie si l'URL est une plateforme (pas le vrai site du producteur)."""
    if not url:
        return True
    domain = urlparse(url).netloc.lower()
    return any(skip in domain for skip in SKIP_DOMAINS)


def _scrape_website_contacts(session: requests.Session, url: str) -> dict:
    """Scrape un site web pour en extraire téléphone et email."""
    contacts = {}
    try:
        time.sleep(1.5)
        resp = session.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        if resp.status_code != 200:
            return contacts
        contacts = _extract_contacts_from_html(resp.text)

        # Si pas trouvé sur la page d'accueil, essayer /contact
        if not contacts.get("telephone") or not contacts.get("email"):
            base = f"{urlparse(resp.url).scheme}://{urlparse(resp.url).netloc}"
            for path in ["/contact", "/nous-contacter", "/contacts", "/contactez-nous"]:
                try:
                    time.sleep(1)
                    resp2 = session.get(base + path, timeout=REQUEST_TIMEOUT, allow_redirects=True)
                    if resp2.status_code == 200:
                        page_contacts = _extract_contacts_from_html(resp2.text)
                        for key, val in page_contacts.items():
                            if val and not contacts.get(key):
                                contacts[key] = val
                        if contacts.get("telephone") and contacts.get("email"):
                            break
                except requests.RequestException:
                    continue

    except requests.RequestException:
        pass
    return contacts


def _extract_contacts_from_html(html: str) -> dict:
    """Extrait téléphone et email d'une page HTML."""
    contacts = {}
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text(" ", strip=True)

    # Téléphone via liens tel:
    phones = []
    for a in soup.select('a[href^="tel:"]'):
        phone = a.get("href", "").replace("tel:", "").strip()
        phone = re.sub(r'[\s.\-]', '', phone)
        if phone and len(phone) >= 10 and phone not in phones:
            phones.append(phone)

    # Téléphone via regex
    if not phones:
        for match in re.findall(r'(?:(?:\+33|0033)\s*[1-9]|0[1-9])[\s.\-]?(?:\d{2}[\s.\-]?){4}', text):
            phone = re.sub(r'[\s.\-]', '', match)
            if phone not in phones:
                phones.append(phone)
    if phones:
        contacts["telephone"] = " / ".join(phones[:2])

    # Email via liens mailto:
    for a in soup.select('a[href^="mailto:"]'):
        email = a.get("href", "").replace("mailto:", "").split("?")[0].strip()
        if email and "@" in email and not email.endswith("example.com"):
            contacts["email"] = email
            break

    # Email via regex
    if "email" not in contacts:
        emails = re.findall(r'[\w.+-]+@[\w-]+\.[\w.-]+', text)
        for email in emails:
            if not any(skip in email.lower() for skip in [
                "example", "sentry", "webpack", "noreply", "wixpress",
                "schema.org", "w3.org", "googleapis", ".png", ".jpg",
            ]):
                contacts["email"] = email
                break

    # Réseaux sociaux
    socials = []
    for a in soup.select('a[href]'):
        href = a.get("href", "")
        for domain in ["facebook.com", "instagram.com"]:
            if domain in href and href not in socials:
                socials.append(href)
    if socials:
        contacts["reseaux_sociaux"] = " | ".join(socials[:3])

    return contacts


def _search_bing(session: requests.Session, nom: str, ville: str) -> tuple[str | None, dict]:
    """Recherche le producteur sur Bing et extrait contacts des résultats."""
    query = f'"{nom}" {ville} téléphone email'
    search_url = f"https://www.bing.com/search?q={quote_plus(query)}&setlang=fr"

    try:
        time.sleep(2)
        resp = session.get(search_url, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            return None, {}

        soup = BeautifulSoup(resp.text, "lxml")

        # Extraire les contacts directement depuis les snippets Bing
        contacts = {}
        full_text = soup.get_text(" ", strip=True)

        # Téléphone dans les résultats Bing
        phones = re.findall(r'(?:0[1-9])[\s.\-]?(?:\d{2}[\s.\-]?){4}', full_text)
        if phones:
            contacts["telephone"] = re.sub(r'[\s.\-]', '', phones[0])

        # Email dans les résultats Bing
        emails = re.findall(r'[\w.+-]+@[\w-]+\.[\w.-]+', full_text)
        for email in emails:
            if not any(skip in email.lower() for skip in [
                "example", "microsoft", "bing", "noreply", "live.com",
                "outlook", "hotmail", ".png", ".jpg",
            ]):
                contacts["email"] = email
                break

        # Extraire les URLs des résultats pour trouver le vrai site
        found_url = None
        result_links = soup.select("li.b_algo h2 a")
        for a in result_links[:5]:
            href = a.get("href", "")
            if not href.startswith("http"):
                continue
            domain = urlparse(href).netloc.lower()
            if any(skip in domain for skip in SKIP_DOMAINS):
                continue
            # C'est probablement le site du producteur
            if not found_url:
                found_url = href
            # Scraper le site pour plus de contacts
            if not contacts.get("telephone") or not contacts.get("email"):
                site_contacts = _scrape_website_contacts(session, href)
                for key, val in site_contacts.items():
                    if val and not contacts.get(key):
                        contacts[key] = val
                if contacts.get("telephone") and contacts.get("email"):
                    break

        return found_url, contacts

    except requests.RequestException:
        return None, {}
