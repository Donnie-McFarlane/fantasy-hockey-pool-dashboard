#!/usr/bin/env python3
"""Require an actual Cloudflare Access challenge on a public test page."""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HOST = "https://batspool2027-private.pages.dev/"


def cloudflare(route):
    account = os.environ["CLOUDFLARE_ACCOUNT_ID"]
    request = urllib.request.Request(
        f"https://api.cloudflare.com/client/v4/accounts/{account}{route}",
        headers={"Authorization": f"Bearer {os.environ['CLOUDFLARE_API_TOKEN']}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.load(response)
    except (OSError, ValueError):
        sys.exit("Cloudflare policy lookup failed; check the deployment token permissions.")
    if not payload.get("success"):
        sys.exit("Cloudflare rejected the policy lookup.")
    return payload["result"]


def verify_policies():
    project = cloudflare("/pages/projects/batspool2027-private")
    if project.get("production_branch") != "main":
        sys.exit("Unexpected Cloudflare production branch.")
    apps = cloudflare("/access/apps?per_page=100")
    hostname = urllib.parse.urlparse(HOST).hostname
    for domain in (hostname, f"*.{hostname}"):
        matches = [app for app in apps if app.get("domain") == domain]
        if len(matches) != 1:
            sys.exit("Expected separate Access applications for production and preview hosts.")
        app_id = matches[0]["id"]
        policies = cloudflare(f"/access/apps/{app_id}/policies?per_page=100")
        allows = [policy for policy in policies if policy.get("decision") == "allow"]
        if not allows or any(policy.get("decision") not in {"allow", "deny"} for policy in policies):
            sys.exit("Access policies do not restrict visitors to the email allowlist.")
        for policy in allows:
            rules = policy.get("include", [])
            if not rules or any(set(rule) != {"email"} or not rule["email"].get("email") for rule in rules):
                sys.exit("Access allow policy must use individual email addresses.")
    print("Verified individual-email Access policies for production and preview.")


verify_policies()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


opener = urllib.request.build_opener(NoRedirect)
for attempt in range(18):
    try:
        req = urllib.request.Request(HOST, headers={"User-Agent": "Mozilla/5.0 (compatible; BatspoolAccessCheck/1.0; +https://github.com/Donnie-McFarlane/fantasy-hockey-pool-dashboard)", "Accept": "text/html,application/xhtml+xml"})
        response = opener.open(req, timeout=12)
    except urllib.error.HTTPError as exc:
        response = exc
    except urllib.error.URLError:
        time.sleep(5)
        continue
    loc = response.headers.get("Location", "")
    print(f"Unauthenticated Access check: HTTP {response.status}; redirect host: {urllib.parse.urlparse(loc).hostname}; server: {response.headers.get('Server')}; content-type: {response.headers.get('Content-Type')}", flush=True)
    if response.status == 403:
        if attempt >= 2:
            sys.exit("Repeated HTTP 403. Login flow not verified; dashboard remains unpublished.")
    if response.status in (301, 302, 303, 307, 308) and ".cloudflareaccess.com" in loc:
        print("Cloudflare Access intercepts unauthenticated requests. Safe to deploy.")
        break
    time.sleep(5)
else:
    sys.exit("No proven Access login barrier. Dashboard deployment blocked.")

