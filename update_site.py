import sqlite3
import pandas as pd
import os
import re
import subprocess
from datetime import datetime
import json

DB_PATH = os.path.join('..', 'tennis_stats.db')
REGISTRE_PATH = os.path.join('..', 'registre_audits.txt')
HTML_PATH = 'index.html'


def generate_pronos_html(df):
    """Génère les cartes des pronostics en cours (grille de 3)"""
    df_en_cours = df[df['Résultat'] == 'En cours'].copy()

    html_cards = '<div class="grid grid-cols-1 md:grid-cols-3 gap-6">'

    if df_en_cours.empty:
        html_cards += '<p class="text-slate-500 text-center col-span-full">Aucun prono public aujourd\'hui</p>'
    else:
        for _, row in df_en_cours.iterrows():
            html_cards += f'''
        <div class="bg-slate-900 border border-emerald-500/30 rounded-2xl p-5 card-hover">
            <div class="flex justify-between items-start mb-4">
                <div>
                    <span class="text-emerald-400 text-xs font-bold uppercase">{row['Tournoi']}</span>
                    <h3 class="text-sm font-bold mt-1">{row['Match']}</h3>
                </div>
            </div>
            
            <div class="bg-slate-950 rounded-xl p-4 mb-4">
                <div class="flex justify-between text-xs">
                    <span class="text-base text-white">Pari :</span>
                    <span class="text-base font-bold text-yellow-300">{row['Pari']}</span>
                </div>
            </div>
            
            <div class="flex justify-between text-xs">
                <div><span class="text-slate-500">Cote</span><br><span class="text-lg font-bold">{row['Cote']:.2f}</span></div>
                <div><span class="text-slate-500">Mise</span><br><span class="text-lg font-bold">{row['Mise']:.2f} €</span></div>
            </div>
        </div>
'''

    html_cards += '''
        <div class="bg-slate-900 border border-slate-700 rounded-2xl p-5 flex flex-col justify-center items-center text-center min-h-[148px] card-hover">
            <div class="text-3xl mb-2 opacity-50">🔒</div>
            <h4 class="text-sm font-bold">Match Premium</h4>
            <p class="text-slate-500 text-[10px]">Réservé aux membres du Club</p>
        </div>
    </div>
'''
    return html_cards


def generate_perf_table(df):
    """Génère le tableau avec une harmonisation complète des polices et tailles."""
    df_terminees = df[df['Résultat'].isin(['Gagné', 'Perdu', 'Annulé'])].sort_values(by='Date', ascending=False)

    if df_terminees.empty:
        return '<p class="text-slate-500 text-center py-10">Aucun historique disponible.</p>'

    rows = ""
    for _, row in df_terminees.iterrows():
        color_class = "text-emerald-500" if row['Résultat'] == 'Gagné' else ("text-red-500" if row['Résultat'] == 'Perdu' else "text-slate-400")

        date_fmt = row['Date'].strftime('%d/%m/%Y')
        cote_fmt = f"{row['Cote']:.2f}"
        gain_fmt = f"{row['Gain/Perte']:.2f} €"

        rows += f'''
        <tr class="border-b border-slate-800 hover:bg-slate-800/30 transition">
            <td class="px-6 py-4 text-center text-xs text-white">{date_fmt}</td>
            <td class="px-6 py-4 text-center text-xs text-white">{row['Match']}</td>
            <td class="px-6 py-4 text-center text-xs text-yellow-300">{row['Pari']}</td>
            <td class="px-6 py-4 text-center text-xs text-white">{cote_fmt}</td>
            <td class="px-6 py-4 text-center text-xs {color_class}">{row['Résultat']}</td>
            <td class="px-6 py-4 text-center text-xs {color_class}">{gain_fmt}</td>
        </tr>'''

    return f'''
    <table class="w-full text-left border-collapse">
        <thead>
            <tr class="text-slate-500 text-xs uppercase tracking-wider border-b border-slate-800">
                <th class="px-6 py-4 text-center text-xs">Date</th>
                <th class="px-6 py-4 text-center text-xs">Match</th>
                <th class="px-6 py-4 text-center text-xs">Pari</th>
                <th class="px-6 py-4 text-center text-xs">Cote</th>
                <th class="px-6 py-4 text-center text-xs">Statut</th>
                <th class="px-6 py-4 text-center text-xs">Gains/Pertes</th>
            </tr>
        </thead>
        <tbody class="text-slate-300">{rows}</tbody>
    </table>'''

def generate_stats_mensuelles(df):
    """Génère le tableau statistique mensuel."""
    # Filtrer uniquement les paris terminés
    df_term = df[df['Résultat'].isin(['Gagné', 'Perdu'])].copy()
    if df_term.empty:
        return ""

    df_term['Mois'] = df_term['Date'].dt.to_period('M')
    stats = df_term.groupby('Mois').apply(lambda x: pd.Series({
        'Nb': len(x),
        'Gagnés': len(x[x['Résultat'] == 'Gagné']),
        'Cote_Moy': x['Cote'].mean(),
        'Yield': (x['Gain/Perte'].sum() / x['Mise'].sum()) * 100 if x['Mise'].sum() != 0 else 0,
        'PNL': x['Gain/Perte'].sum()
    })).reset_index()

    stats = stats.sort_values(by='Mois', ascending=False)
    
    rows = ""
    for _, row in stats.iterrows():
        nb_paris = int(row['Nb'])
        nb_gagnes = int(row['Gagnés'])
        
        rows += f'''
        <tr class="border-b border-slate-800 last:border-b-0 text-xs">
            <td class="px-6 py-3 text-center text-xs text-white">{row['Mois'].strftime('%m/%Y')}</td>
            <td class="px-6 py-3 text-center text-xs text-white">{nb_paris}</td>
            <td class="px-6 py-3 text-center text-xs text-white">{nb_gagnes}</td>
            <td class="px-6 py-3 text-center text-xs text-white">{(row['Gagnés']/row['Nb']*100):.1f}%</td>
            <td class="px-6 py-3 text-center text-xs text-white">{row['Cote_Moy']:.2f}</td>
            <td class="px-6 py-3 text-center text-xs text-white">{row['Yield']:.1f}%</td>
            <td class="px-6 py-3 text-center text-xs {'text-emerald-500' if row['PNL'] >= 0 else 'text-red-500'}">{row['PNL']:.2f} €</td>
        </tr>'''

    return f'''
    <div class="overflow-x-auto mb-10">
        <table class="w-full text-left border-collapse bg-slate-900 rounded-xl overflow-hidden">
            <thead class="text-slate-500 text-xs uppercase tracking-wider border-b border-slate-800">
                <tr>
                    <th class="px-6 py-4 text-center text-xs">Mois</th>
                    <th class="px-6 py-4 text-center text-xs">Nombre paris</th>
                    <th class="px-6 py-4 text-center text-xs">Nombre gagnés</th>
                    <th class="px-6 py-4 text-center text-xs">Taux de réussite</th>
                    <th class="px-6 py-4 text-center text-xs">Cote Moyenne</th>
                    <th class="px-6 py-4 text-center text-xs">Yield</th>
                    <th class="px-6 py-4 text-center text-xs">Gains/Pertes</th>
                </tr>
            </thead>
            <tbody class="text-slate-300 divide-y divide-slate-800">{rows}</tbody>
        </table>
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


def get_audit_html(db_path):
    """Génère la section complète du registre d'audit"""
    audit_rows = []
    audit_entries = []

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
                    audit_rows.append(
                        f'<tr><td class="px-4 py-3 text-white text-center text-xs">{d}</td>'
                        f'<td class="px-4 py-3 text-white text-xs text-center">{p}</td>'
                        f'<td class="px-4 py-3 text-yellow-600 font-mono text-xs font-bold text-center">{h}</td>'
                        f'<td class="px-4 py-3 text-emerald-500 text-center">✓</td></tr>'
                    )
    except FileNotFoundError:
        return '<tr><td colspan="4" class="text-center py-4 text-slate-500">Registre introuvable.</td></tr>'

    row_data = get_verification_data(db_path)
    row_data_json = json.dumps(row_data, ensure_ascii=True)
    audit_entries_json = json.dumps(audit_entries, ensure_ascii=True)

    options_html = "".join([
        f'<option value="{i}">{e["date"]} — {e["pari"]}</option>'
        for i, e in enumerate(audit_entries)
    ])

    return f'''
    <div class="max-w-6xl mx-auto overflow-x-auto bg-slate-900 border border-slate-800 rounded-2xl shadow-xl">
        <div class="px-4 py-2 bg-slate-950 border-b border-slate-800 text-emerald-400 text-[10px] font-bold uppercase tracking-widest text-center">[Vérification en cours : Système intègre]</div>
        <table class="min-w-full divide-y divide-slate-700 text-xs">
            <thead class="bg-slate-950">
                <tr>
                    <th class="px-4 py-3 text-emerald-400 text-center">Date</th>
                    <th class="px-4 py-3 text-emerald-400 text-center">Pari</th>
                    <th class="px-4 py-3 text-emerald-400 text-center">Hash</th>
                    <th class="px-4 py-3 text-emerald-400 text-center">Statut</th>
                </tr>
            </thead>
            <tbody class="divide-y divide-slate-800">{"".join(audit_rows)}</tbody>
        </table>
    </div>
    <div class="max-w-6xl mx-auto mt-8 p-6 bg-slate-950 border border-slate-800 rounded-xl">
        <h3 class="text-emerald-400 font-bold mb-4 text-lg">Comment fonctionne l'audit cryptographique ?</h3>
        <div class="text-slate-400 text-sm space-y-4">
            <p>- <b>Principe</b> : chaque pari est scellé dans une chaîne où le Hash résulte d'un calcul combinant tout l'historique des paris et l'empreinte du pari précédent</p>
            <p>- <b>Immutabilité</b> : si une seule donnée est modifiée dans le passé, le Hash de cette ligne change</p>
            <p>- <b>Vérifiabilité</b> : choisissez un pari ci-dessous pour vérifier l'intégrité en direct</p>
        </div>
        <div class="mt-8 p-6 bg-slate-900 border border-emerald-900/30 rounded-lg">
            <h4 class="text-emerald-400 font-bold text-sm mb-4 uppercase">Vérificateur d'intégrité</h4>
            <select id="select-verif" class="bg-black border border-slate-700 text-white p-2 rounded text-xs w-full mb-4">{options_html}</select>
            <button onclick="verifierHash()" class="bg-emerald-600 hover:bg-emerald-500 text-white px-6 py-2 rounded text-sm font-bold transition">Vérifier l'intégrité</button>
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
    conn = sqlite3.connect(DB_PATH)
    query = """
        SELECT nom_tournoi AS 'Tournoi', date_match AS 'Date', match_intitule AS 'Match', 
               joueur_choisi AS 'Pari', mise AS 'Mise', cote_jouee AS 'Cote', 
               statut AS 'Résultat', gain_net AS 'Gain/Perte' 
        FROM Historique_Paris
    """
    df = pd.read_sql_query(query, conn)
    conn.close()
    df['Date'] = pd.to_datetime(df['Date'], format='mixed', utc=True)

    pronos_html = generate_pronos_html(df)
    stats_html = generate_stats_mensuelles(df)
    perf_html = generate_perf_table(df)
    audit_html = get_audit_html(DB_PATH)
    maj_date_html = generate_maj_date()

    if os.path.exists(HTML_PATH):
        with open(HTML_PATH, 'r', encoding='utf-8') as f:
            contenu = f.read()

        contenu = re.sub(r'<!-- Début des paris en cours -->.*?<!-- Fin des paris en cours -->', f'<!-- Début des paris en cours -->\n{pronos_html}\n<!-- Fin des paris en cours -->', contenu, flags=re.DOTALL)
        contenu = re.sub(r'<!-- Début des statistiques mensuelles -->.*?<!-- Fin des statistiques mensuelles -->', f'<!-- Début des statistiques mensuelles -->\n{stats_html}\n<!-- Fin des statistiques mensuelles -->', contenu, flags=re.DOTALL)
        contenu = re.sub(r'<!-- Début des performances -->.*?<!-- Fin des performances -->', f'<!-- Début des performances -->\n{perf_html}\n<!-- Fin des performances -->', contenu, flags=re.DOTALL)
        contenu = re.sub(r'<!-- Début du registre -->.*?<!-- Fin du registre -->', f'<!-- Début du registre -->\n{audit_html}\n<!-- Fin du registre -->', contenu, flags=re.DOTALL)
        contenu = re.sub(r'<!-- Début date maj -->.*?<!-- Fin date maj -->', f'<!-- Début date maj -->{maj_date_html}<!-- Fin date maj -->', contenu, flags=re.DOTALL)

        with open(HTML_PATH, 'w', encoding='utf-8') as f:
            f.write(contenu)

    push_to_github()

if __name__ == "__main__":
    mettre_a_jour_site()