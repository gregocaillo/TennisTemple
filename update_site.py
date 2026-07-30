import sqlite3
import pandas as pd
import os
import re
import subprocess
import requests
import smtplib
from email.mime.text import MIMEText
from datetime import datetime, timezone
import json

DB_PATH = os.path.join('..', 'tennis_stats.db')
REGISTRE_PATH = os.path.join('..', 'registre_audits.txt')
MATCHS_JSON_PATH = os.path.join('..', 'matchs_atp_unibet.json')
HTML_PATH = 'index.html'

# Charge les clés depuis secrets_local.py s'il existe (fichier non versionné, voir .gitignore).
# Sinon, les variables d'environnement système classiques prennent le relais.
try:
    import secrets_local
except ImportError:
    pass

# ==== CONFIG SUPABASE ====
# La clé SERVICE (secret) ne doit JAMAIS être commitée sur GitHub.
# Définissez-la en variable d'environnement avant de lancer le script :
#   export SUPABASE_URL="https://eldkwsoucmfgvofsvshc.supabase.co"
#   export SUPABASE_SERVICE_KEY="sb_secret_..."
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://eldkwsoucmfgvofsvshc.supabase.co")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")

# ==== CONFIG NOTIFICATION EMAIL (nouvelles inscriptions) ====
# Compte Gmail utilisé pour ENVOYER l'email (nécessite un "mot de passe d'application" Gmail,
# pas votre mot de passe habituel — voir instructions fournies séparément).
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
EMAIL_DESTINATAIRE = os.environ.get("EMAIL_DESTINATAIRE", GMAIL_ADDRESS)

DERNIER_CHECK_PATH = os.path.join('..', 'dernier_check_inscriptions.txt')


def lire_nombre_matchs_analyses():
    """Compte le nombre de matchs présents dans le fichier JSON d'analyse (liste de matchs)."""
    try:
        with open(MATCHS_JSON_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return len(data) if isinstance(data, list) else 0
    except (FileNotFoundError, json.JSONDecodeError):
        return 0


def generate_pronos_stats_banner(df):
    """Génère le bandeau de stats génériques affiché au-dessus des cartes de pronostics."""
    nb_matchs_analyses = lire_nombre_matchs_analyses()

    df_en_cours = df[df['Résultat'] == 'En cours'].copy()
    nb_paris_proposes = len(df_en_cours)

    df_avec_prob = df_en_cours.dropna(subset=['ProbPredite'])
    if not df_avec_prob.empty:
        valeurs = (df_avec_prob['Cote'] * df_avec_prob['ProbPredite'] - 1) * 100
        value_moyenne = valeurs.mean()
        value_txt = f"+{value_moyenne:.1f}%" if value_moyenne >= 0 else f"{value_moyenne:.1f}%"
        value_couleur = "text-emerald-400" if value_moyenne >= 0 else "text-red-400"
    else:
        value_txt = "—"
        value_couleur = "text-white"

    return f'''
    <div class="grid grid-cols-1 sm:grid-cols-3 gap-3 sm:gap-4 mb-6 sm:mb-8">
        <div class="bg-slate-900 border gold-frame rounded-2xl p-4 sm:p-5 text-center">
            <p class="text-slate-500 text-[10px] uppercase tracking-wider mb-2">Matchs analysés</p>
            <p class="text-xl sm:text-2xl font-bold text-white">{nb_matchs_analyses}</p>
        </div>
        <div class="bg-slate-900 border gold-frame rounded-2xl p-4 sm:p-5 text-center">
            <p class="text-slate-500 text-[10px] uppercase tracking-wider mb-2">Paris proposés</p>
            <p id="stat-paris-proposes" class="text-xl sm:text-2xl font-bold text-white">{nb_paris_proposes}</p>
        </div>
        <div class="bg-slate-900 border gold-frame rounded-2xl p-4 sm:p-5 text-center">
            <p class="text-slate-500 text-[10px] uppercase tracking-wider mb-2">Value Bet moyen</p>
            <p id="stat-value-bet" class="text-xl sm:text-2xl font-bold {value_couleur}">{value_txt}</p>
        </div>
    </div>'''


def appliquer_clotures_admin():
    """Récupère les clôtures de paris décidées depuis /admin.html (statut Gagné/Perdu/Annulé)
    et les applique à la base SQLite locale, qui reste la source de vérité."""
    if not SUPABASE_SERVICE_KEY:
        print("⚠ SUPABASE_SERVICE_KEY non définie, synchronisation des clôtures admin ignorée.")
        return

    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    }

    resp = requests.get(f"{SUPABASE_URL}/rest/v1/resultats_clotures", headers=headers, params={"select": "*"})
    if resp.status_code >= 300:
        print(f"⚠ Erreur lors de la récupération des clôtures admin : {resp.status_code} {resp.text}")
        return

    clotures = resp.json()
    if not clotures:
        print("Aucune clôture en attente depuis l'espace admin.")
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    ids_traites = []

    for c in clotures:
        cursor.execute(
            "UPDATE Historique_Paris SET statut = ?, gain_net = ? WHERE id = ?",
            (c['statut'], c['gain_net'], c['paris_id'])
        )
        ids_traites.append(c['id'])
        print(f"Clôture admin appliquée : pari #{c['paris_id']} → {c['statut']} ({c['gain_net']:+.2f} €)")

    conn.commit()
    conn.close()

    # Nettoie la table de staging une fois les clôtures appliquées localement,
    # pour ne pas les réappliquer au prochain passage.
    for cid in ids_traites:
        del_resp = requests.delete(f"{SUPABASE_URL}/rest/v1/resultats_clotures?id=eq.{cid}", headers=headers)
        if del_resp.status_code >= 300:
            print(f"⚠ Erreur lors du nettoyage de la clôture #{cid} : {del_resp.status_code}")

    print(f"{len(ids_traites)} clôture(s) admin appliquée(s) et synchronisée(s) avec la base locale.")

def push_pronos_premium(df):
    """Met à jour la table pronos_premium sur Supabase (upsert) sans recréer les lignes
    existantes, afin de préserver leur date de création d'origine."""
    if not SUPABASE_SERVICE_KEY:
        print("⚠ SUPABASE_SERVICE_KEY non définie, envoi des pronos premium ignoré.")
        return

    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json"
    }

    df_en_cours = df[df['Résultat'] == 'En cours']
    ids_en_cours = df_en_cours['ID'].astype(int).tolist()

    # 1. Supprime uniquement les paris qui NE SONT PLUS en cours (clôturés entre-temps)
    if ids_en_cours:
        ids_str = ",".join(str(i) for i in ids_en_cours)
        filtre = f"paris_id=not.in.({ids_str})"
    else:
        filtre = "paris_id=gt.0"  # plus aucun pari en cours -> table à vider entièrement
    del_resp = requests.delete(f"{SUPABASE_URL}/rest/v1/pronos_premium?{filtre}", headers=headers)
    if del_resp.status_code >= 300:
        print(f"⚠ Erreur lors du nettoyage de pronos_premium : {del_resp.status_code} {del_resp.text}")

    # 2. Upsert des paris en cours : insère les nouveaux, met à jour les existants
    #    SANS toucher à created_at (colonne non envoyée dans le payload)
    upsert_headers = {**headers, "Prefer": "resolution=merge-duplicates"}
    payloads = []
    for _, row in df_en_cours.iterrows():
        payloads.append({
            "paris_id": int(row['ID']),
            "tournoi": row['Tournoi'],
            "match_intitule": row['Match'],
            "pari": row['Pari'],
            "cote": float(row['Cote']),
            "mise": float(row['Mise']),
            "gain_potentiel": float(row['GainPotentiel']) if pd.notna(row['GainPotentiel']) else None,
            # Nécessaire pour que le site recalcule "Value Bet moyen" en direct côté client,
            # à partir des mêmes lignes que celles affichées en cartes (voir index.html /
            # chargerPronos), et non depuis un bandeau HTML figé qui peut devenir périmé
            # entre deux exécutions de ce script.
            "prob_predite": float(row['ProbPredite']) if pd.notna(row['ProbPredite']) else None
        })

    if payloads:
        resp = requests.post(
            f"{SUPABASE_URL}/rest/v1/pronos_premium?on_conflict=paris_id",
            json=payloads,
            headers=upsert_headers
        )
        if resp.status_code >= 300:
            print(f"⚠ Erreur lors de l'upsert des pronos premium : {resp.status_code} {resp.text}")

    print(f"{len(payloads)} prono(s) premium synchronisé(s) avec Supabase (upsert).")

def push_analyses_generales(db_path):
    """Met à jour la table analyses_generales sur Supabase avec les analyses du jour
    (Analyses_Totales), retenues ou non comme value bet. Visible uniquement aux membres
    connectés — la restriction d'accès (RLS) doit être configurée côté Supabase sur
    cette table, comme c'est déjà le cas pour pronos_premium."""
    if not SUPABASE_SERVICE_KEY:
        print("⚠ SUPABASE_SERVICE_KEY non définie, envoi des analyses générales ignoré.")
        return

    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json"
    }

    conn = sqlite3.connect(db_path)
    date_auj = datetime.now().strftime('%Y-%m-%d')
    # Analyses_Totales : une ligne = un candidat retenu (joueur_choisi/cote_jouee/
    # prob_predite), plus statut_analyse (VB_valide / VB_suspect / Pas_de_value).
    query = """
        SELECT id, nom_tournoi, match_intitule, joueur_choisi, cote_jouee,
               prob_predite, statut_analyse, date_match
        FROM Analyses_Totales
        WHERE date_match LIKE ?
    """
    df = pd.read_sql_query(query, conn, params=(date_auj + '%',))
    conn.close()

    ids_du_jour = df['id'].astype(int).tolist()

    # 1. Supprime tout ce qui n'est plus dans les analyses d'aujourd'hui (jours précédents inclus)
    if ids_du_jour:
        ids_str = ",".join(str(i) for i in ids_du_jour)
        filtre = f"analyse_id=not.in.({ids_str})"
    else:
        filtre = "analyse_id=gt.0"  # aucune analyse aujourd'hui -> table à vider entièrement
    del_resp = requests.delete(f"{SUPABASE_URL}/rest/v1/analyses_generales?{filtre}", headers=headers)
    if del_resp.status_code >= 300:
        print(f"⚠ Erreur lors du nettoyage de analyses_generales : {del_resp.status_code} {del_resp.text}")

    # 2. Upsert des analyses du jour
    upsert_headers = {**headers, "Prefer": "resolution=merge-duplicates"}
    payloads = []
    for _, row in df.iterrows():
        cote_calculee = (1 / row['prob_predite']) if pd.notna(row['prob_predite']) and row['prob_predite'] else None
        payloads.append({
            "analyse_id": int(row['id']),
            "tournoi": row['nom_tournoi'],
            "match_intitule": row['match_intitule'],
            "joueur_choisi": row['joueur_choisi'],
            "cote_calculee": cote_calculee,
            "cote_marche": float(row['cote_jouee']) if pd.notna(row['cote_jouee']) else None,
            "statut_analyse": row['statut_analyse'] if pd.notna(row['statut_analyse']) else None,
            "date_match": row['date_match'] if pd.notna(row['date_match']) else None,
        })

    if payloads:
        resp = requests.post(
            f"{SUPABASE_URL}/rest/v1/analyses_generales?on_conflict=analyse_id",
            json=payloads,
            headers=upsert_headers
        )
        if resp.status_code >= 300:
            print(f"⚠ Erreur lors de l'upsert des analyses générales : {resp.status_code} {resp.text}")
        else:
            print(f"{len(payloads)} analyse(s) du jour synchronisée(s) avec Supabase.")
    else:
        print("Aucune analyse du jour à synchroniser.")

def generate_perf_table(df):
    """Génère le tableau desktop + la vue en cartes mobile, avec pastilles de statut.

    Pour éviter un scroll interminable, l'historique complet est embarqué en JSON dans la
    page (masqué), mais seuls les 5 matchs les plus récents sont affichés au chargement.
    Un bouton "Plus de matchs" révèle 5 matchs supplémentaires à chaque clic, jusqu'à un
    maximum de 20 affichés. Au-delà (ou si tout l'historique tient déjà à l'écran), ce
    bouton est remplacé par un bouton d'export Excel qui télécharge la totalité de
    l'historique (généré côté navigateur avec SheetJS, aucun fichier serveur nécessaire)."""
    df_terminees = df[df['Résultat'].isin(['Gagné', 'Perdu', 'Annulé'])].sort_values(by='Date', ascending=False)

    if df_terminees.empty:
        return '<p class="text-slate-500 text-center py-10">Aucun historique disponible.</p>'

    matchs = []
    for _, row in df_terminees.iterrows():
        matchs.append({
            "date": row['Date'].strftime('%d/%m/%Y'),
            "match": row['Match'],
            "pari": row['Pari'],
            "cote": f"{row['Cote']:.2f}",
            "cote_prematch": f"{row['CotePrematch']:.2f}" if pd.notna(row['CotePrematch']) else "—",
            "statut": row['Résultat'],
            "gain": f"{row['Gain/Perte']:.2f}",
        })

    # ensure_ascii=False pour garder les accents lisibles dans la source ; on échappe les
    # "</" pour ne jamais risquer de fermer prématurément la balise <script> qui contient ce JSON.
    matchs_json = json.dumps(matchs, ensure_ascii=False).replace('</', '<\\/')

    return f'''
    <script type="application/json" id="perf-data">{matchs_json}</script>

    <div class="hidden md:block overflow-x-auto">
        <table class="w-full text-left border-collapse">
            <thead>
                <tr class="text-slate-500 text-xs uppercase tracking-wider border-b border-slate-700">
                    <th class="px-6 py-4 text-center text-xs">Date</th>
                    <th class="px-6 py-4 text-center text-xs">Match</th>
                    <th class="px-6 py-4 text-center text-xs">Pari</th>
                    <th class="px-6 py-4 text-center text-xs">Cote Jouée</th>
                    <th class="px-6 py-4 text-center text-xs">Cote Pré-match</th>
                    <th class="px-6 py-4 text-center text-xs">Statut</th>
                    <th class="px-6 py-4 text-center text-xs">Gains/Pertes</th>
                </tr>
            </thead>
            <tbody id="perf-table-body" class="text-slate-300"></tbody>
        </table>
    </div>
    <div id="perf-cards" class="md:hidden space-y-3"></div>

    <div class="flex justify-center mt-8">
        <button id="perf-load-more-btn" onclick="perfChargerPlus()" type="button"
            class="inline-flex items-center gap-2 bg-slate-900 border gold-frame text-slate-200 text-xs font-bold uppercase tracking-wider px-6 py-3 rounded-full hover:bg-slate-800 transition">
            <i class="fa-solid fa-chevron-down"></i>
            <span>Plus de matchs</span>
        </button>
        <button id="perf-export-btn" onclick="perfExporterExcel()" type="button"
            class="hidden inline-flex items-center gap-2 bg-gradient-to-r from-violet-700 via-fuchsia-500 to-amber-400 text-white text-xs font-bold uppercase tracking-wider px-6 py-3 rounded-full hover:opacity-90 transition">
            <i class="fa-solid fa-file-excel"></i>
            <span>Exporter tout l'historique (Excel)</span>
        </button>
    </div>

    <script>
    (function() {{
        const PALIER = 5;
        const MAX_AFFICHES = 20;
        const tousLesMatchs = JSON.parse(document.getElementById('perf-data').textContent);
        let nbAffiches = 0;

        function badgeStatut(statut) {{
            if (statut === 'Gagné') {{
                return '<span class="inline-flex items-center gap-1 px-2.5 py-1 rounded-full text-[10px] font-bold bg-emerald-500/20 text-emerald-400"><i class="fa-solid fa-check text-[9px]"></i>Gagné</span>';
            }}
            if (statut === 'Perdu') {{
                return '<span class="inline-flex items-center gap-1 px-2.5 py-1 rounded-full text-[10px] font-bold bg-red-500/20 text-red-400"><i class="fa-solid fa-xmark text-[9px]"></i>Perdu</span>';
            }}
            return '<span class="inline-flex items-center gap-1 px-2.5 py-1 rounded-full text-[10px] font-bold bg-slate-700/40 text-slate-400"><i class="fa-solid fa-minus text-[9px]"></i>Annulé</span>';
        }}

        function classeCouleur(statut) {{
            if (statut === 'Gagné') return 'text-emerald-500';
            if (statut === 'Perdu') return 'text-red-500';
            return 'text-slate-400';
        }}

        function echapperHtml(texte) {{
            const div = document.createElement('div');
            div.textContent = texte;
            return div.innerHTML;
        }}

        function perfChargerPlus() {{
            const tbody = document.getElementById('perf-table-body');
            const cardsDiv = document.getElementById('perf-cards');
            const prochain = tousLesMatchs.slice(nbAffiches, nbAffiches + PALIER);

            prochain.forEach(m => {{
                const badge = badgeStatut(m.statut);
                const cls = classeCouleur(m.statut);
                const match = echapperHtml(m.match);
                const pari = echapperHtml(m.pari);

                tbody.insertAdjacentHTML('beforeend', `
                <tr class="border-b border-slate-700 hover:bg-slate-800/30 transition">
                    <td class="px-6 py-4 text-center text-xs text-white">${{m.date}}</td>
                    <td class="px-6 py-4 text-center text-xs text-white">${{match}}</td>
                    <td class="px-6 py-4 text-center text-xs text-yellow-300">${{pari}}</td>
                    <td class="px-6 py-4 text-center text-xs text-white">${{m.cote}}</td>
                    <td class="px-6 py-4 text-center text-xs text-slate-400">${{m.cote_prematch}}</td>
                    <td class="px-6 py-4 text-center text-xs">${{badge}}</td>
                    <td class="px-6 py-4 text-center text-xs ${{cls}}">${{m.gain}} €</td>
                </tr>`);

                cardsDiv.insertAdjacentHTML('beforeend', `
                <div class="bg-slate-900 border gold-frame rounded-2xl p-4">
                    <div class="flex justify-between items-start mb-2">
                        <span class="text-xs text-slate-500">${{m.date}}</span>
                        ${{badge}}
                    </div>
                    <p class="text-sm font-bold text-white mb-1">${{match}}</p>
                    <p class="text-xs text-yellow-300 mb-3">${{pari}} <span class="text-slate-500">@ ${{m.cote}} (pré-match ${{m.cote_prematch}})</span></p>
                    <p class="text-sm font-bold ${{cls}}">${{m.gain}} €</p>
                </div>`);
            }});

            nbAffiches += prochain.length;
            mettreAJourBoutons();
        }}

        function mettreAJourBoutons() {{
            const btnPlus = document.getElementById('perf-load-more-btn');
            const btnExport = document.getElementById('perf-export-btn');
            const resteDesMatchs = nbAffiches < tousLesMatchs.length;
            const palierMaxAtteint = nbAffiches >= MAX_AFFICHES;

            if (resteDesMatchs && !palierMaxAtteint) {{
                btnPlus.classList.remove('hidden');
                btnExport.classList.add('hidden');
            }} else {{
                btnPlus.classList.add('hidden');
                btnExport.classList.remove('hidden');
            }}
        }}

        function perfExporterExcel() {{
            if (typeof XLSX === 'undefined') {{
                showToast("Impossible de générer le fichier Excel pour le moment, réessayez dans un instant.");
                return;
            }}
            const entetes = ["Date", "Match", "Pari", "Cote jouée", "Cote pré-match", "Statut", "Gain/Perte (€)"];
            const donnees = [entetes].concat(tousLesMatchs.map(m => [
                m.date, m.match, m.pari, m.cote, m.cote_prematch, m.statut, m.gain
            ]));
            const feuille = XLSX.utils.aoa_to_sheet(donnees);
            feuille['!cols'] = [{{wch: 12}}, {{wch: 42}}, {{wch: 22}}, {{wch: 12}}, {{wch: 14}}, {{wch: 10}}, {{wch: 14}}];
            const classeur = XLSX.utils.book_new();
            XLSX.utils.book_append_sheet(classeur, feuille, "Historique");
            const horodatage = new Date().toISOString().slice(0, 10);
            XLSX.writeFile(classeur, `the-winning-oracle-historique-${{horodatage}}.xlsx`);
        }}

        window.perfChargerPlus = perfChargerPlus;
        window.perfExporterExcel = perfExporterExcel;

        perfChargerPlus();
    }})();
    </script>'''

def generate_stats_mensuelles(df):
    """Génère le tableau statistique mensuel (desktop) + les cartes équivalentes (mobile)."""
    # Filtrer uniquement les paris terminés
    df_term = df[df['Résultat'].isin(['Gagné', 'Perdu'])].copy()
    if df_term.empty:
        return ""

    df_term['Mois'] = df_term['Date'].dt.to_period('M')
    stats = df_term.groupby('Mois').apply(lambda x: pd.Series({
        'Nb': len(x),
        'Gagnés': len(x[x['Résultat'] == 'Gagné']),
        'Cote_Moy': x['Cote'].mean(),
        'CLV_Moy': x['CLV'].dropna().mean() if x['CLV'].dropna().shape[0] > 0 else None,
        'Yield': (x['Gain/Perte'].sum() / x['Mise'].sum()) * 100 if x['Mise'].sum() != 0 else 0,
        'PNL': x['Gain/Perte'].sum()
    })).reset_index()

    stats = stats.sort_values(by='Mois', ascending=False)

    rows = ""
    cards = ""
    for _, row in stats.iterrows():
        nb_paris = int(row['Nb'])
        nb_gagnes = int(row['Gagnés'])
        taux_reussite = row['Gagnés'] / row['Nb'] * 100
        pnl_color = 'text-emerald-500' if row['PNL'] >= 0 else 'text-red-500'
        if pd.notna(row['CLV_Moy']):
            clv_txt = f"+{row['CLV_Moy']:.1f}%" if row['CLV_Moy'] >= 0 else f"{row['CLV_Moy']:.1f}%"
            clv_color = 'text-emerald-500' if row['CLV_Moy'] >= 0 else 'text-red-500'
        else:
            clv_txt = "—"
            clv_color = 'text-slate-500'
        mois_fmt = row['Mois'].strftime('%m/%Y')

        rows += f'''
        <tr class="border-b border-slate-700 last:border-b-0 text-xs">
            <td class="px-6 py-3 text-center text-xs text-white">{mois_fmt}</td>
            <td class="px-6 py-3 text-center text-xs text-white">{nb_paris}</td>
            <td class="px-6 py-3 text-center text-xs text-white">{nb_gagnes}</td>
            <td class="px-6 py-3 text-center text-xs text-white">{taux_reussite:.1f}%</td>
            <td class="px-6 py-3 text-center text-xs text-white">{row['Cote_Moy']:.2f}</td>
            <td class="px-6 py-3 text-center text-xs {clv_color}">{clv_txt}</td>
            <td class="px-6 py-3 text-center text-xs text-white">{row['Yield']:.1f}%</td>
            <td class="px-6 py-3 text-center text-xs {pnl_color}">{row['PNL']:.2f} €</td>
        </tr>'''

        cards += f'''
        <div class="bg-slate-900 border gold-frame rounded-2xl p-4">
            <div class="flex justify-between items-center mb-3">
                <span class="text-sm font-bold text-white">{mois_fmt}</span>
                <span class="text-sm font-bold {pnl_color}">{row['PNL']:.2f} €</span>
            </div>
            <div class="flex flex-col gap-y-2 text-xs">
                <p class="text-slate-500 flex justify-between"><span>Paris</span> <span class="text-white">&nbsp;&nbsp;{nb_paris}&nbsp;&nbsp;</span></p>
                <p class="text-slate-500 flex justify-between"><span>Gagnés</span> <span class="text-white">&nbsp;&nbsp;{nb_gagnes}&nbsp;&nbsp;</span></p>
                <p class="text-slate-500 flex justify-between"><span>Réussite</span> <span class="text-white">&nbsp;&nbsp;{taux_reussite:.1f}%&nbsp;&nbsp;</span></p>
                <p class="text-slate-500 flex justify-between"><span>Cote moy.</span> <span class="text-white">&nbsp;&nbsp;{row['Cote_Moy']:.2f}&nbsp;&nbsp;</span></p>
                <p class="text-slate-500 flex justify-between"><span>CLV moy.</span> <span class="{clv_color}">&nbsp;&nbsp;{clv_txt}&nbsp;&nbsp;</span></p>
                <p class="text-slate-500 flex justify-between"><span>Yield</span> <span class="text-white">&nbsp;&nbsp;{row['Yield']:.1f}%&nbsp;&nbsp;</span></p>
            </div>
        </div>'''

    table_html = f'''
    <div class="hidden md:block overflow-x-auto mb-10">
        <table class="w-full text-left border-collapse bg-slate-900 rounded-xl overflow-hidden">
            <thead class="text-slate-500 text-xs uppercase tracking-wider border-b border-slate-700">
                <tr>
                    <th class="px-6 py-4 text-center text-xs">Mois</th>
                    <th class="px-6 py-4 text-center text-xs">Nombre paris</th>
                    <th class="px-6 py-4 text-center text-xs">Nombre gagnés</th>
                    <th class="px-6 py-4 text-center text-xs">Taux de réussite</th>
                    <th class="px-6 py-4 text-center text-xs">Cote Jouée Moyenne</th>
                    <th class="px-6 py-4 text-center text-xs" title="Closing Line Value : écart moyen (%) entre la cote jouée et la cote pré-match. Positif = vous avez obtenu une meilleure cote que le marché.">CLV Moyen</th>
                    <th class="px-6 py-4 text-center text-xs">Yield</th>
                    <th class="px-6 py-4 text-center text-xs">Gains/Pertes</th>
                </tr>
            </thead>
            <tbody class="text-slate-300 divide-y divide-slate-800">{rows}</tbody>
        </table>
    </div>'''

    cards_html = f'''
    <div class="md:hidden space-y-3 mb-10">{cards}</div>'''

    return table_html + cards_html

def generate_sparkline_svg(valeurs, dates=None):
    """Génère un mini graphique SVG (sparkline) à partir d'une liste de valeurs
    cumulées, avec grille horizontale et légendes des axes (€ en ordonnée,
    dates ou n° de pari en abscisse).

    valeurs : liste de soldes cumulés (float), du plus ancien au plus récent.
    dates   : liste optionnelle de labels alignés sur `valeurs` (ex: '17/07'),
              utilisée pour l'abscisse. Si absente -> 'Pari 1' / 'Pari N'.
    """
    if len(valeurs) < 2:
        return ''

    largeur, hauteur, marge = 300, 100, 8
    v_min, v_max = min(valeurs), max(valeurs)
    if v_max == v_min:
        v_max += 1
    n = len(valeurs)

    def _x(i):
        return marge + (i / (n - 1)) * (largeur - 2 * marge)

    def _y(v):
        return hauteur - marge - ((v - v_min) / (v_max - v_min)) * (hauteur - 2 * marge)

    points = [f"{_x(i):.1f},{_y(v):.1f}" for i, v in enumerate(valeurs)]
    polyline = " ".join(points)
    couleur = "#34d399" if valeurs[-1] >= valeurs[0] else "#f87171"

    # --- Grille horizontale : 4 repères également espacés entre min et max ---
    nb_reperes = 4
    lignes_grille = []
    for i in range(nb_reperes):
        fraction = i / (nb_reperes - 1)
        valeur_repere = v_min + fraction * (v_max - v_min)
        y = _y(valeur_repere)
        lignes_grille.append(
            f'<line x1="{marge}" y1="{y:.1f}" x2="{largeur - marge}" y2="{y:.1f}" '
            f'stroke="#1e293b" stroke-width="1"/>'
        )
    grille_svg = "\n            ".join(lignes_grille)

    # --- Ligne du zéro (si comprise dans la plage) ---
    ligne_zero = ''
    if v_min <= 0 <= v_max:
        zero_y = _y(0)
        ligne_zero = (
            f'<line x1="{marge}" y1="{zero_y:.1f}" x2="{largeur - marge}" y2="{zero_y:.1f}" '
            f'stroke="#334155" stroke-width="1" stroke-dasharray="3,3"/>'
        )

    # --- Labels de l'abscisse : dates si fournies, sinon "Pari 1" / "Pari N" ---
    if dates and len(dates) == n:
        label_debut, label_fin = dates[0], dates[-1]
    else:
        label_debut, label_fin = "Pari 1", f"Pari {n}"

    return f'''
    <div>
        <div class="relative">
            <span class="absolute top-0 left-1 text-[8px] text-slate-600 bg-slate-900/80 px-1 rounded">{v_max:+.0f}€</span>
            <span class="absolute bottom-0 left-1 text-[8px] text-slate-600 bg-slate-900/80 px-1 rounded">{v_min:+.0f}€</span>
            <svg viewBox="0 0 {largeur} {hauteur}" preserveAspectRatio="none" class="w-full h-16">
                {grille_svg}
                {ligne_zero}
                <polyline points="{polyline}" fill="none" stroke="{couleur}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
            </svg>
        </div>
        <div class="flex justify-between text-[8px] text-slate-600 uppercase tracking-wider mt-1 pt-1 border-t border-slate-700/60">
            <span>{label_debut}</span>
            <span>Solde (€)</span>
            <span>{label_fin}</span>
        </div>
    </div>'''

def generate_stats_banner(df):
    """Génère le bandeau de statistiques clés (taux de réussite, série, CLV, PnL, cote moyenne) + sparkline."""
    df_term = df[df['Résultat'].isin(['Gagné', 'Perdu'])].sort_values(by='Date', ascending=True).copy()

    if df_term.empty:
        return ''

    total = len(df_term)
    gagnes = len(df_term[df_term['Résultat'] == 'Gagné'])
    taux_reussite = (gagnes / total) * 100 if total else 0
    cote_moyenne = df_term['Cote'].mean()

    clv_valides = df_term['CLV'].dropna()
    if not clv_valides.empty:
        clv_moyen = clv_valides.mean()
        clv_txt = f"+{clv_moyen:.1f}%" if clv_moyen >= 0 else f"{clv_moyen:.1f}%"
        clv_couleur = "text-emerald-400" if clv_moyen >= 0 else "text-red-400"
    else:
        clv_txt = "—"
        clv_couleur = "text-white"

    df_pnl = df[df['Résultat'].isin(['Gagné', 'Perdu', 'Annulé'])].sort_values(by='Date', ascending=True)
    pnl_total = df_pnl['Gain/Perte'].sum()

    # ROI global : gains/pertes cumulés rapportés au total des mises engagées (même
    # périmètre que le PNL cumulé ci-dessus : Gagné / Perdu / Annulé).
    mises_total = df_pnl['Mise'].sum()
    roi_global = (pnl_total / mises_total) * 100 if mises_total else 0
    roi_txt = f"+{roi_global:.1f}%" if roi_global >= 0 else f"{roi_global:.1f}%"
    roi_couleur = "text-emerald-400" if roi_global >= 0 else "text-red-400"

    # Série en cours : on part du pari le plus récent et on compte tant que le résultat est identique
    df_recent_first = df_term.sort_values(by='Date', ascending=False)
    serie_resultat = df_recent_first.iloc[0]['Résultat']
    serie_longueur = 0
    for _, row in df_recent_first.iterrows():
        if row['Résultat'] == serie_resultat:
            serie_longueur += 1
        else:
            break
    serie_texte = f"{serie_longueur} {'Gagné' if serie_resultat == 'Gagné' else 'Perdu'}{'s' if serie_longueur > 1 else ''}"
    serie_emoji = "🔥" if serie_resultat == 'Gagné' and serie_longueur >= 2 else ("❄️" if serie_resultat == 'Perdu' and serie_longueur >= 2 else "")
    serie_couleur = "text-emerald-400" if serie_resultat == 'Gagné' else "text-red-400"

    # Sparkline sur TOUT l'historique disponible (plus de limite aux 30 derniers paris),
    # avec les vraies dates de chaque pari pour la légende de l'abscisse.
    cumul = df_pnl['Gain/Perte'].cumsum().tolist()
    dates_paris = df_pnl['Date'].dt.strftime('%d/%m/%y').tolist()
    sparkline = generate_sparkline_svg(cumul, dates=dates_paris)

    pnl_couleur = "text-emerald-400" if pnl_total >= 0 else "text-red-400"

    clv_explication = (
        "Closing Line Value : écart en % entre la cote à laquelle vous avez parié et la cote "
        "pré-match (utilisée comme référence du marché juste avant le match). "
        "Un CLV positif signifie que vous avez obtenu une meilleure cote que le marché — "
        "c'est un indicateur de la qualité de votre timing/valeur, indépendant du résultat du match."
    )

    return f'''
    <div class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-3 sm:gap-4 mb-8 sm:mb-10">
        <div class="bg-slate-900 border gold-frame rounded-2xl p-4 sm:p-5">
            <p class="text-slate-500 text-[10px] text-center uppercase tracking-wider mb-2">Taux de réussite</p>
            <p class="text-xl sm:text-2xl font-bold text-center text-white">{taux_reussite:.1f}%</p>
        </div>
        <div class="bg-slate-900 border gold-frame rounded-2xl p-4 sm:p-5">
            <p class="text-slate-500 text-[10px] text-center uppercase tracking-wider mb-2">Série en cours</p>
            <p class="text-lg sm:text-2xl font-bold text-center {serie_couleur} truncate">{serie_emoji} {serie_texte}</p>
        </div>
        <div class="bg-slate-900 border gold-frame rounded-2xl p-4 sm:p-5">
            <p class="text-slate-500 text-[10px] text-center uppercase tracking-wider mb-2 cursor-help inline-flex items-center gap-1 justify-center w-full" title="{clv_explication}">CLV Moyen <i class="fa-solid fa-circle-info text-[9px]"></i></p>
            <p class="text-xl sm:text-2xl font-bold text-center {clv_couleur}">{clv_txt}</p>
        </div>
        <div class="bg-slate-900 border gold-frame rounded-2xl p-4 sm:p-5">
            <p class="text-slate-500 text-[10px] text-center uppercase tracking-wider mb-2">Gains/Pertes cumulés</p>
            <p class="text-xl sm:text-2xl font-bold text-center {pnl_couleur}">{pnl_total:.2f} €</p>
        </div>
        <div class="bg-slate-900 border gold-frame rounded-2xl p-4 sm:p-5">
            <p class="text-slate-500 text-[10px] text-center uppercase tracking-wider mb-2">Cote Jouée Moyenne</p>
            <p class="text-xl sm:text-2xl font-bold text-center text-white">{cote_moyenne:.2f}</p>
        </div>
        <div class="col-span-2 sm:col-span-1 bg-slate-900 border gold-frame rounded-2xl p-4 sm:p-5">
            <p class="text-slate-500 text-[10px] text-center uppercase tracking-wider mb-2 cursor-help inline-flex items-center gap-1 justify-center w-full" title="Retour sur investissement global : gains/pertes cumulés rapportés au total des mises engagées.">ROI <i class="fa-solid fa-circle-info text-[9px]"></i></p>
            <p class="text-xl sm:text-2xl font-bold text-center {roi_couleur}">{roi_txt}</p>
        </div>
    </div>
    <div class="bg-slate-900 border gold-frame rounded-2xl p-4 sm:p-5 mb-8 sm:mb-10">
        <p class="text-slate-500 text-[10px] text-center uppercase tracking-wider mb-3">Évolution du solde (Nombre de paris : {len(cumul)})</p>
        {sparkline}
    </div>'''


def generate_maj_date():
    """Génère le texte 'Dernière mise à jour' avec la date du jour"""
    return f"Dernière mise à jour : {datetime.now().strftime('%d/%m/%Y')}"


def get_verification_data(db_path):
    """Récupère les données brutes des paris pour la vérification du hash"""
    conn = sqlite3.connect(db_path)
    # On récupère les données dans le même ordre que lors de la création du hash
    query = "SELECT nom_tournoi, date_match, match_intitule, joueur_choisi, cote_jouee, mise FROM Historique_Paris ORDER BY rowid ASC"
    cursor = conn.execute(query)
    data = [str(row) for row in cursor.fetchall()]
    conn.close()
    return data


def get_statuts_ordonnes(db_path):
    """Récupère le statut de chaque pari dans le même ordre (rowid ASC) que get_verification_data,
    pour pouvoir déterminer, à partir du 'Lignes: N' d'une entrée du registre, si le pari
    correspondant (le N-ième ajouté) est toujours 'En cours' ou non."""
    conn = sqlite3.connect(db_path)
    cursor = conn.execute("SELECT statut FROM Historique_Paris ORDER BY rowid ASC")
    statuts = [row[0] for row in cursor.fetchall()]
    conn.close()
    return statuts


def get_audit_html(db_path):
    """Génère la section complète du registre d'audit : tableau desktop + cartes mobile.
    Seuls les paris déjà clôturés (statut != 'En cours') sont affichés dans le tableau
    et le menu déroulant : un pari encore en cours ne doit pas être visible publiquement
    (il ne reste accessible que via l'espace premium/Supabase). La chaîne de hash complète
    (AUDIT_ENTRIES / AUDIT_ROWS_DATA) reste néanmoins intacte côté JS pour que le calcul
    d'intégrité des paris déjà affichés (dont le hash dépend de tout l'historique) reste correct.
    """
    audit_entries = []  # liste complète, y compris les paris "En cours" (nécessaire à la chaîne de hash)

    try:
        with open(REGISTRE_PATH, 'r', encoding='utf-8') as f:
            for line in f:
                if not line.strip():
                    continue
                parts = [p.strip() for p in line.split('|')]
                if len(parts) >= 3:
                    d = parts[0].replace('Date: ', '')
                    p = parts[1].replace('Pari: ', '')
                    h = parts[2].replace('Hash: ', '')
                    nrows = None
                    if len(parts) >= 4 and parts[3].startswith('Lignes: '):
                        try:
                            nrows = int(parts[3].replace('Lignes: ', ''))
                        except ValueError:
                            nrows = None

                    audit_entries.append({'date': d, 'pari': p, 'hash': h, 'nrows': nrows})
    except FileNotFoundError:
        return '<tr><td colspan="4" class="text-center py-4 text-slate-500">Registre introuvable.</td></tr>'

    statuts_ordonnes = get_statuts_ordonnes(db_path)

    def pari_encore_en_cours(nrows):
        # nrows = nombre de lignes en base au moment où l'entrée a été scellée,
        # donc le pari correspondant est le N-ième ajouté (rowid ASC), en position N-1.
        if nrows is None or nrows < 1 or nrows > len(statuts_ordonnes):
            return False  # cas limite : on préfère afficher plutôt que masquer à tort
        return statuts_ordonnes[nrows - 1] == 'En cours'

    audit_rows_all = []
    audit_cards_all = []
    options_html_parts = []
    nb_masques = 0
    
    for i, e in enumerate(audit_entries):
        if pari_encore_en_cours(e['nrows']):
            nb_masques += 1
            continue
            
        audit_rows_all.append(
            f'<tr><td class="px-4 py-3 text-white text-center text-xs">{e["date"]}</td>'
            f'<td class="px-4 py-3 text-white text-xs text-center">{e["pari"]}</td>'
            f'<td class="px-4 py-3 text-yellow-600 font-mono text-xs font-bold text-center">{e["hash"]}</td>'
            f'<td class="px-4 py-3 text-emerald-500 text-center">✓</td></tr>'
        )
        audit_cards_all.append(f'''
        <div class="bg-slate-950 border gold-frame rounded-xl p-4">
            <div class="flex justify-between items-center mb-2">
                <span class="text-xs text-white">{e["date"]}</span>
                <span class="text-emerald-500 text-xs">✓ Intègre</span>
            </div>
            <p class="text-xs text-white mb-2">{e["pari"]}</p>
            <p class="text-yellow-600 font-mono text-[10px] font-bold break-all">{e["hash"]}</p>
        </div>''')
        options_html_parts.append(f'<option value="{i}">{e["date"]} — {e["pari"]}</option>')

    # Filtrage : ne garder que les 5 derniers pour l'affichage
    audit_rows = audit_rows_all[-5:]
    audit_cards = audit_cards_all[-5:]

    if not audit_rows:
        audit_rows_html = '<tr><td colspan="4" class="text-center py-6 text-slate-500">Aucun pari clôturé pour le moment.</td></tr>'
        audit_cards_html = '<p class="text-slate-500 text-center py-6 text-sm">Aucun pari clôturé pour le moment.</p>'
    else:
        audit_rows_html = "".join(audit_rows)
        audit_cards_html = "".join(audit_cards)

    note_masques_html = (
        f'<p class="text-slate-600 text-[10px] text-center mt-3">{nb_masques} pari(s) en cours scellé(s) '
        f'mais non affiché(s) publiquement tant qu\'ils ne sont pas clôturés.</p>'
        if nb_masques > 0 else ''
    )

    row_data = get_verification_data(db_path)
    row_data_json = json.dumps(row_data, ensure_ascii=True)
    audit_entries_json = json.dumps(audit_entries, ensure_ascii=True)

    options_html = "".join(options_html_parts)

    return f'''
    <div class="max-w-6xl mx-auto bg-slate-900 border gold-frame rounded-2xl shadow-xl">
        <div class="px-4 py-2 bg-slate-950 border-b border-slate-700 rounded-t-2xl text-emerald-400 text-[10px] font-bold uppercase tracking-widest text-center">[Vérification en cours : Système intègre]</div>

        <div class="px-4 pt-4 pb-0">
            <p class="text-slate-500 text-[10px] italic text-center">Affichage limité aux 5 derniers certificats clôturés.</p>
        </div>

        <div class="hidden md:block overflow-x-auto">
            <table class="min-w-full divide-y divide-slate-700 text-xs">
                <thead class="bg-slate-950">
                    <tr>
                        <th class="px-4 py-3 text-emerald-400 text-center">Date</th>
                        <th class="px-4 py-3 text-emerald-400 text-center">Pari</th>
                        <th class="px-4 py-3 text-emerald-400 text-center">Hash</th>
                        <th class="px-4 py-3 text-emerald-400 text-center">Statut</th>
                    </tr>
                </thead>
                <tbody class="divide-y divide-slate-800">{audit_rows_html}</tbody>
            </table>
        </div>
        <div class="md:hidden p-4 space-y-3">{audit_cards_html}</div>
    </div>
    {note_masques_html}
    <div class="max-w-6xl mx-auto mt-8 p-4 sm:p-6 bg-slate-950 border gold-frame rounded-xl">
        <h3 class="text-emerald-400 font-bold mb-4 text-base sm:text-lg">Comment fonctionne l'audit cryptographique ?</h3>
        <div class="text-slate-400 text-sm space-y-4">
            <p>- <b>Principe</b> : chaque pari est scellé dans une chaîne où le Hash résulte d'un calcul combinant tout l'historique des paris et l'empreinte du pari précédent</p>
            <p>- <b>Immutabilité</b> : si une seule donnée est modifiée dans le passé, le Hash de cette ligne change</p>
            <p>- <b>Vérifiabilité</b> : choisissez un pari ci-dessous pour vérifier l'intégrité en direct</p>
        </div>
        <div class="mt-8 p-4 sm:p-6 bg-slate-900 border gold-frame rounded-lg">
            <h4 class="text-emerald-400 font-bold text-sm mb-4 uppercase">Vérificateur d'intégrité</h4>
            <select id="select-verif" class="bg-black border gold-frame text-white p-2 rounded text-xs w-full mb-4">{options_html}</select>
            <button onclick="verifierHash()" class="w-full sm:w-auto bg-emerald-600 hover:bg-emerald-500 text-white px-6 py-2 rounded text-sm font-bold transition">Vérifier l'intégrité</button>
            <div id="resultat-hash" class="mt-4 p-3 bg-black border border-emerald-900 rounded text-[11px] font-mono break-all hidden"></div>
        </div>
    </div>
    <script>
        const AUDIT_ROWS_DATA = {row_data_json};
        const AUDIT_ENTRIES = {audit_entries_json};
        async function verifierHash() {{
            const idx = parseInt(document.getElementById('select-verif').value, 10);
            const entry = AUDIT_ENTRIES[idx];
            const divRes = document.getElementById('resultat-hash');
            divRes.classList.remove('hidden');
            if (entry.nrows === null) {{ 
                divRes.textContent = "⚠ Vérification impossible."; 
                return; 
            }}
            const subset = AUDIT_ROWS_DATA.slice(0, entry.nrows);
            const prevHash = idx === 0 ? "0" : AUDIT_ENTRIES[idx - 1].hash;
            const chaine = subset.join('') + prevHash;
            const msg = new TextEncoder().encode(chaine);
            const hashBuffer = await crypto.subtle.digest('SHA-256', msg);
            const hashCalcule = Array.from(new Uint8Array(hashBuffer)).map(b => b.toString(16).padStart(2, '0')).join('');
            if (hashCalcule === entry.hash) {{ 
                divRes.className = "mt-4 p-3 bg-black border border-emerald-900 rounded text-[11px] font-mono break-all text-emerald-500"; 
                divRes.textContent = "✓ Intègre : " + hashCalcule; 
            }} else {{ 
                divRes.className = "mt-4 p-3 bg-black border border-red-900 rounded text-[11px] font-mono break-all text-red-500"; 
                divRes.textContent = "✗ ANOMALIE : " + hashCalcule; 
            }}
        }}
    </script>
    '''

def lire_dernier_check():
    """Lit la date du dernier passage. Si le fichier n'existe pas, on part de maintenant
    (pour ne pas spammer avec tous les comptes déjà existants au premier lancement)."""
    if os.path.exists(DERNIER_CHECK_PATH):
        with open(DERNIER_CHECK_PATH, 'r') as f:
            return f.read().strip()
    return datetime.now(timezone.utc).isoformat()


def ecrire_dernier_check(horodatage):
    with open(DERNIER_CHECK_PATH, 'w') as f:
        f.write(horodatage)


def envoyer_email_notification(nouveaux_emails):
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        print("⚠ GMAIL_ADDRESS / GMAIL_APP_PASSWORD non définis, notification email ignorée.")
        return

    corps = "Nouvelle(s) inscription(s) sur The Winning Oracle :\n\n" + "\n".join(f"- {e}" for e in nouveaux_emails)
    corps += "\n\nActivez leur accès premium (après paiement) depuis /admin.html"

    msg = MIMEText(corps)
    msg['Subject'] = f"The Winning Oracle : {len(nouveaux_emails)} nouvelle(s) inscription(s)"
    msg['From'] = GMAIL_ADDRESS
    msg['To'] = EMAIL_DESTINATAIRE

    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as serveur:
            serveur.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            serveur.send_message(msg)
        print(f"Email de notification envoyé ({len(nouveaux_emails)} inscription(s)).")
    except Exception as e:
        print(f"⚠ Erreur lors de l'envoi de l'email : {e}")


def verifier_nouvelles_inscriptions():
    """Interroge Supabase pour les comptes créés depuis le dernier passage du script,
    et envoie un email si des nouveaux comptes sont trouvés."""
    if not SUPABASE_SERVICE_KEY:
        print("⚠ SUPABASE_SERVICE_KEY non définie, vérification des inscriptions ignorée.")
        return

    dernier_check = lire_dernier_check()
    maintenant = datetime.now(timezone.utc).isoformat()

    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    }
    params = {
        "select": "email,created_at",
        "created_at": f"gt.{dernier_check}",
        "order": "created_at.asc"
    }

    resp = requests.get(f"{SUPABASE_URL}/rest/v1/profiles", headers=headers, params=params)

    if resp.status_code >= 300:
        print(f"⚠ Erreur lors de la vérification des inscriptions : {resp.status_code} {resp.text}")
        return

    nouveaux = resp.json()

    if nouveaux:
        emails = [p['email'] for p in nouveaux]
        print(f"{len(emails)} nouvelle(s) inscription(s) détectée(s) : {', '.join(emails)}")
        envoyer_email_notification(emails)
    else:
        print("Aucune nouvelle inscription depuis le dernier passage.")

    ecrire_dernier_check(maintenant)


def push_to_github(repo_path='.', commit_message=None):
    """Commit et push les changements sur GitHub, uniquement s'il y en a."""
    if commit_message is None:
        commit_message = f"Mise à jour auto - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"

    try:
        subprocess.run(['git', 'add', '.'], cwd=repo_path, check=True)

        status = subprocess.run(
            ['git', 'status', '--porcelain'],
            cwd=repo_path, capture_output=True, text=True, check=True
        )
        if not status.stdout.strip():
            print("Aucun changement à pousser.")
            return

        subprocess.run(['git', 'commit', '-m', commit_message], cwd=repo_path, check=True)
        subprocess.run(['git', 'push'], cwd=repo_path, check=True)
        print("Mise à jour poussée sur GitHub avec succès.")
    except subprocess.CalledProcessError as e:
        print(f"Erreur lors du push GitHub : {e}")

def mettre_a_jour_site():
    # Applique d'abord les clôtures de paris décidées depuis /admin.html, avant
    # de régénérer quoi que ce soit à partir de la base (qui doit être à jour).
    appliquer_clotures_admin()

    conn = sqlite3.connect(DB_PATH)
    query = """
        SELECT id AS 'ID', nom_tournoi AS 'Tournoi', date_match AS 'Date', match_intitule AS 'Match', 
               joueur_choisi AS 'Pari', mise AS 'Mise', cote_jouee AS 'Cote', cote_prematch AS 'CotePrematch',
               statut AS 'Résultat', gain_net AS 'Gain/Perte', prob_predite AS 'ProbPredite',
               gain_potentiel AS 'GainPotentiel'
        FROM Historique_Paris
    """
    df = pd.read_sql_query(query, conn)
    conn.close()
    df['Date'] = pd.to_datetime(df['Date'], format='mixed', utc=True)

    # CLV (Closing Line Value) : écart en % entre la cote jouée et la cote pré-match
    # (utilisée comme proxy de la "closing line"). Positif = meilleure cote que le marché.
    df['CLV'] = None
    mask_clv = df['CotePrematch'].notna() & (df['CotePrematch'] != 0)
    df.loc[mask_clv, 'CLV'] = (df.loc[mask_clv, 'Cote'] / df.loc[mask_clv, 'CotePrematch'] - 1) * 100

    # Les pronos "En cours" ne sont plus écrits dans le HTML : ils partent vers Supabase,
    # où seuls les comptes premium authentifiés peuvent les lire (voir index.html).
    push_pronos_premium(df)
    pronos_stats_html = generate_pronos_stats_banner(df)

    # Les analyses générales du jour (Analyses_Totales) partent aussi vers Supabase :
    # visibles uniquement aux membres connectés (voir index.html), jamais écrites en dur
    # dans le HTML public.
    push_analyses_generales(DB_PATH)

    # Vérifie si de nouveaux visiteurs se sont inscrits depuis le dernier passage,
    # et vous envoie un email si c'est le cas.
    verifier_nouvelles_inscriptions()

    stats_html = generate_stats_mensuelles(df)
    banniere_html = generate_stats_banner(df)
    perf_html = generate_perf_table(df)
    audit_html = get_audit_html(DB_PATH)
    maj_date_html = generate_maj_date()

    if os.path.exists(HTML_PATH):
        with open(HTML_PATH, 'r', encoding='utf-8') as f:
            contenu = f.read()

# IMPORTANT : on passe des lambdas comme argument `repl` de re.sub().
        # Quand `repl` est une chaîne, re.sub() la traite comme un template et
        # interprète les backslashes (\1, \g<name>, \u...) de façon spéciale.
        # Or audit_html contient du JSON encodé avec ensure_ascii=True, qui produit
        # des séquences \uXXXX pour les caractères accentués (é, ô, î...) — d'où
        # l'erreur "bad escape \u". Une fonction comme repl est utilisée telle quelle,
        # sans interprétation des backslashes.
        contenu = re.sub(r'<!-- Début stats pronos -->.*?<!-- Fin stats pronos -->', lambda m: f'<!-- Début stats pronos -->\n{pronos_stats_html}\n<!-- Fin stats pronos -->', contenu, flags=re.DOTALL)
        contenu = re.sub(r'<!-- Début bandeau stats -->.*?<!-- Fin bandeau stats -->', lambda m: f'<!-- Début bandeau stats -->\n{banniere_html}\n<!-- Fin bandeau stats -->', contenu, flags=re.DOTALL)
        contenu = re.sub(r'<!-- Début des statistiques mensuelles -->.*?<!-- Fin des statistiques mensuelles -->', lambda m: f'<!-- Début des statistiques mensuelles -->\n{stats_html}\n<!-- Fin des statistiques mensuelles -->', contenu, flags=re.DOTALL)
        contenu = re.sub(r'<!-- Début des performances -->.*?<!-- Fin des performances -->', lambda m: f'<!-- Début des performances -->\n{perf_html}\n<!-- Fin des performances -->', contenu, flags=re.DOTALL)
        contenu = re.sub(r'<!-- Début du registre -->.*?<!-- Fin du registre -->', lambda m: f'<!-- Début du registre -->\n{audit_html}\n<!-- Fin du registre -->', contenu, flags=re.DOTALL)
        contenu = re.sub(r'<!-- Début date maj -->.*?<!-- Fin date maj -->', lambda m: f'<!-- Début date maj -->{maj_date_html}<!-- Fin date maj -->', contenu, flags=re.DOTALL)

        with open(HTML_PATH, 'w', encoding='utf-8') as f:
            f.write(contenu)

    push_to_github()

if __name__ == "__main__":
    mettre_a_jour_site()