import requests, os

SUPABASE_URL = "https://eldkwsoucmfgvofsvshc.supabase.co"
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]

headers = {
    "apikey": SUPABASE_SERVICE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "resolution=merge-duplicates"
}

test_payload = [{"paris_id": 999999, "tournoi": "Test", "match_intitule": "A vs B", "pari": "A", "cote": 1.5, "mise": 10}]

r = requests.post(f"{SUPABASE_URL}/rest/v1/pronos_premium?on_conflict=paris_id", json=test_payload, headers=headers)
print(r.status_code, r.text)