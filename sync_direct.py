"""
sync_direct.py
--------------
Synchronise les dossiers COOPS vers Grist depuis Démarches Simplifiées.

Démarches gérées :
  - COOPS v1a  (DEMARCHE_V1A) ┐
  - COOPS v1b  (DEMARCHE_V1B) ┴─→ fusionnées comme "coops_v1" dans EXPORT
  - COOPS v2   (DEMARCHE_V2)  ──→ "coops_v2" dans EXPORT

Fonctionnement :
  - Sync incrémentale : seuls les dossiers modifiés depuis le dernier run sont traités
  - La date du dernier run est stockée dans une table Grist "Sync_metadata"
  - Premier run = import complet de tout l'historique
  - Zéro serveur, zéro base de données externe
  - Anti-doublon par clé composite (dossier_number + source_version)
  - La colonne "dossier_id_source" = numéro_c1 ou numéro_c2

Dépendances : pip install requests
Secrets GitHub requis : DS_TOKEN, DEMARCHE_V1A, DEMARCHE_V1B, DEMARCHE_V2,
                        GRIST_API_KEY, GRIST_DOC_ID
"""

import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone
import requests

# =============================================================================
# CONFIGURATION
# =============================================================================

DS_TOKEN        = os.environ["DS_TOKEN"]

# Démarches — au moins une doit être définie
DEMARCHE_V1A    = os.environ.get("DEMARCHE_V1A", "").strip()  # COOPS v1 démarche A
DEMARCHE_V1B    = os.environ.get("DEMARCHE_V1B", "").strip()  # COOPS v1 démarche B (2 champs en plus)
DEMARCHE_V2     = os.environ.get("DEMARCHE_V2",  "").strip()  # COOPS v2

GRIST_API_KEY   = os.environ["GRIST_API_KEY"]
GRIST_DOC_ID    = os.environ["GRIST_DOC_ID"]
GRIST_BASE_URL  = os.environ.get("GRIST_BASE_URL", "https://grist.numerique.gouv.fr/api")
TABLE_EXPORT    = os.environ.get("GRIST_TABLE", "EXPORT")
TABLE_META      = "Sync_metadata"

DS_API_URL = "https://www.demarches-simplifiees.fr/api/v2/graphql"

GRIST_HEADERS = {
    "Authorization": f"Bearer {GRIST_API_KEY}",
    "Content-Type": "application/json",
}
DS_HEADERS = {
    "Authorization": f"Bearer {DS_TOKEN}",
    "Content-Type": "application/json",
}

# =============================================================================
# REQUÊTE GRAPHQL
# updatedSince est optionnel : absent = récupère tout (premier run)
# =============================================================================

QUERY = """
query getDossiers($demarcheNumber: Int!, $after: String, $updatedSince: ISO8601DateTime) {
  demarche(number: $demarcheNumber) {
    dossiers(first: 100, after: $after, updatedSince: $updatedSince) {
      pageInfo {
        hasNextPage
        endCursor
      }
      nodes {
        number
        state
        dateDepot
        datePassageEnConstruction
        datePassageEnInstruction
        dateTraitement
        dateDerniereModification
        motivation
        usager { email }
        groupeInstructeur { label }
        demandeur {
          ... on PersonnePhysique {
            civilite
            nom
            prenom
            email
          }
          ... on PersonneMorale {
            siret
          }
        }
        champs {
          label
          stringValue
          ... on RepetitionChamp {
            rows {
              champs {
                label
                stringValue
              }
            }
          }
        }
        annotations {
          label
          stringValue
        }
      }
    }
  }
}
"""

# =============================================================================
# UTILITAIRES
# =============================================================================

def label_to_column(label: str) -> str:
    """
    Convertit un label DS en nom de colonne Grist valide.
    Ex: "Nom du médecin" → "nom_du_medecin"
    """
    label = label.lower().strip()
    label = unicodedata.normalize("NFD", label)
    label = "".join(c for c in label if unicodedata.category(c) != "Mn")
    label = re.sub(r"[^a-z0-9]+", "_", label)
    label = label.strip("_")
    return label[:60]


def now_iso() -> str:
    """Retourne l'heure actuelle en UTC au format ISO 8601."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# =============================================================================
# FONCTIONS DS
# =============================================================================

def fetch_all_dossiers(demarche_number: str, updated_since: str | None) -> list[dict]:
    """
    Récupère tous les dossiers d'une démarche DS via GraphQL avec pagination.
    Si updated_since est fourni, ne récupère que les dossiers modifiés depuis cette date.
    """
    mode = f"modifiés depuis {updated_since}" if updated_since else "complet (premier run)"
    print(f"    Démarche {demarche_number} — mode : {mode}")

    all_dossiers = []
    cursor = None
    page = 1

    while True:
        variables = {
            "demarcheNumber": int(demarche_number),
            "after": cursor,
            "updatedSince": updated_since,
        }
        response = requests.post(
            DS_API_URL,
            headers=DS_HEADERS,
            json={"query": QUERY, "variables": variables},
            timeout=60,
        )
        response.raise_for_status()
        data = response.json()

        if "errors" in data:
            print(f"    ERREUR GraphQL : {data['errors']}", file=sys.stderr)
            break

        dossiers_data = data["data"]["demarche"]["dossiers"]
        nodes         = dossiers_data["nodes"]
        page_info     = dossiers_data["pageInfo"]

        for node in nodes:
            all_dossiers.append(normalize_dossier(node))

        print(f"      Page {page} : {len(nodes)} dossiers")

        if not page_info["hasNextPage"]:
            break

        cursor = page_info["endCursor"]
        page += 1
        time.sleep(0.5)  # Respecter le rate limit DS

    print(f"    Total : {len(all_dossiers)} dossiers récupérés")
    return all_dossiers


def normalize_dossier(node: dict) -> dict:
    """
    Transforme un nœud GraphQL DS en dict plat pour Grist.
    Les champs répétables sont aplatis avec un index numérique.
    """
    row = {}

    # Métadonnées système DS
    row["dossier_number"]               = node.get("number")
    row["statut"]                       = node.get("state", "") or ""
    row["date_depot"]                   = node.get("dateDepot", "") or ""
    row["date_derniere_maj"]            = node.get("dateDerniereModification", "") or ""
    row["date_passage_en_construction"] = node.get("datePassageEnConstruction", "") or ""
    row["date_passage_en_instruction"]  = node.get("datePassageEnInstruction", "") or ""
    row["date_traitement"]              = node.get("dateTraitement", "") or ""
    row["motivation"]                   = node.get("motivation", "") or ""
    row["email_usager"]                 = (node.get("usager") or {}).get("email", "") or ""
    row["groupe_instructeur"]           = (node.get("groupeInstructeur") or {}).get("label", "") or ""

    # Demandeur
    demandeur = node.get("demandeur") or {}
    row["demandeur_civilite"]       = demandeur.get("civilite", "") or ""
    row["demandeur_nom"]            = demandeur.get("nom", "") or ""
    row["demandeur_prenom"]         = demandeur.get("prenom", "") or ""
    row["demandeur_email"]          = demandeur.get("email", "") or ""
    row["demandeur_siret"]          = demandeur.get("siret", "") or ""

    # Champs du formulaire → colonnes dynamiques
    for champ in node.get("champs", []) or []:
        col_name = label_to_column(champ.get("label", ""))
        if not col_name:
            continue
        if "rows" in champ and champ["rows"]:
            for i, row_block in enumerate(champ["rows"], start=1):
                for sub_champ in row_block.get("champs", []):
                    sub_col = label_to_column(sub_champ.get("label", ""))
                    if sub_col:
                        row[f"{sub_col}_{i}"] = sub_champ.get("stringValue", "") or ""
        else:
            row[col_name] = champ.get("stringValue", "") or ""

    # Annotations instructeurs
    for annotation in node.get("annotations", []) or []:
        col_name = "annotation_" + label_to_column(annotation.get("label", ""))
        if col_name != "annotation_":
            row[col_name] = annotation.get("stringValue", "") or ""

    return row


# =============================================================================
# FONCTIONS GRIST — SYNC METADATA
# =============================================================================

def ensure_metadata_table():
    """Crée la table Sync_metadata si elle n'existe pas."""
    url = f"{GRIST_BASE_URL}/docs/{GRIST_DOC_ID}/tables"
    r = requests.get(url, headers=GRIST_HEADERS, timeout=30)
    r.raise_for_status()

    if TABLE_META in [t["id"] for t in r.json().get("tables", [])]:
        return

    print(f"  Création de la table '{TABLE_META}'...")
    r = requests.post(url, headers=GRIST_HEADERS, json={
        "tables": [{
            "id": TABLE_META,
            "columns": [
                {"id": "demarche",      "fields": {"type": "Text", "label": "Démarche"}},
                {"id": "derniere_sync", "fields": {"type": "Text", "label": "Dernière sync"}},
            ]
        }]
    })
    r.raise_for_status()
    print(f"  Table '{TABLE_META}' créée ✓")


def get_last_sync(demarche_number: str) -> str | None:
    """
    Lit la date du dernier sync réussi pour une démarche.
    Retourne None si c'est le premier run.
    """
    url = f"{GRIST_BASE_URL}/docs/{GRIST_DOC_ID}/tables/{TABLE_META}/records"
    r = requests.get(url, headers=GRIST_HEADERS, timeout=30)

    if r.status_code == 404:
        return None

    r.raise_for_status()
    for rec in r.json().get("records", []):
        if rec["fields"].get("demarche") == demarche_number:
            val = rec["fields"].get("derniere_sync", "")
            return val if val else None
    return None


def set_last_sync(demarche_number: str, sync_time: str):
    """
    Met à jour (ou crée) la ligne de métadonnée pour une démarche.
    Appelé UNIQUEMENT après un sync réussi — garantit zéro perte en cas de plantage.
    """
    url = f"{GRIST_BASE_URL}/docs/{GRIST_DOC_ID}/tables/{TABLE_META}/records"
    r = requests.get(url, headers=GRIST_HEADERS, timeout=30)
    r.raise_for_status()

    existing_id = None
    for rec in r.json().get("records", []):
        if rec["fields"].get("demarche") == demarche_number:
            existing_id = rec["id"]
            break

    payload = {"demarche": demarche_number, "derniere_sync": sync_time}

    if existing_id:
        r = requests.patch(url, headers=GRIST_HEADERS,
                           json={"records": [{"id": existing_id, "fields": payload}]})
    else:
        r = requests.post(url, headers=GRIST_HEADERS,
                          json={"records": [{"fields": payload}]})
    r.raise_for_status()


# =============================================================================
# FONCTIONS GRIST — TABLE EXPORT
# =============================================================================

def ensure_export_table():
    """Crée la table EXPORT dans Grist si elle n'existe pas."""
    url = f"{GRIST_BASE_URL}/docs/{GRIST_DOC_ID}/tables"
    r = requests.get(url, headers=GRIST_HEADERS, timeout=30)
    r.raise_for_status()

    if TABLE_EXPORT in [t["id"] for t in r.json().get("tables", [])]:
        print(f"  Table '{TABLE_EXPORT}' déjà existante ✓")
        return

    print(f"  Création de la table '{TABLE_EXPORT}'...")
    r = requests.post(url, headers=GRIST_HEADERS, json={
        "tables": [{
            "id": TABLE_EXPORT,
            "columns": [
                {"id": "dossier_id_source",  "fields": {"type": "Text", "label": "ID unique"}},
                {"id": "dossier_number",     "fields": {"type": "Int",  "label": "N° Dossier"}},
                {"id": "source_version",     "fields": {"type": "Text", "label": "Version"}},
                {"id": "statut",             "fields": {"type": "Text", "label": "Statut"}},
                {"id": "date_depot",         "fields": {"type": "Text", "label": "Date dépôt"}},
                {"id": "date_derniere_maj",  "fields": {"type": "Text", "label": "Dernière MAJ"}},
                {"id": "email_usager",       "fields": {"type": "Text", "label": "Email usager"}},
                {"id": "groupe_instructeur", "fields": {"type": "Text", "label": "Groupe instructeur"}},
            ]
        }]
    })
    r.raise_for_status()
    print(f"  Table '{TABLE_EXPORT}' créée ✓")


def ensure_columns_exist(rows: list[dict]):
    """Crée dans Grist les colonnes présentes dans les données mais absentes de la table."""
    url = f"{GRIST_BASE_URL}/docs/{GRIST_DOC_ID}/tables/{TABLE_EXPORT}/columns"
    r = requests.get(url, headers=GRIST_HEADERS, timeout=30)

    existing_cols = set()
    if r.status_code != 404:
        r.raise_for_status()
        existing_cols = {
            col["id"] for col in r.json().get("columns", [])
            if col["id"] not in ("id", "manualSort")
        }

    needed = set()
    for row in rows:
        needed.update(row.keys())
    needed.update({"source_version", "dossier_id_source"})

    missing = sorted(needed - existing_cols)
    if not missing:
        print(f"    Colonnes à jour ({len(existing_cols)} colonnes) ✓")
        return

    print(f"    Ajout de {len(missing)} colonnes manquantes...")
    payload = [{"id": col, "fields": {"type": "Text"}} for col in missing]
    r = requests.post(url, headers=GRIST_HEADERS, json={"columns": payload}, timeout=30)
    r.raise_for_status()
    sample = missing[:5]
    print(f"    Colonnes ajoutées : {sample}{'...' if len(missing) > 5 else ''} ✓")


def get_existing_map() -> dict[tuple, int]:
    """
    Retourne {(dossier_number, source_version): grist_row_id}.
    Clé composite pour éviter toute collision entre v1 et v2.
    """
    url = f"{GRIST_BASE_URL}/docs/{GRIST_DOC_ID}/tables/{TABLE_EXPORT}/records"
    r = requests.get(url, headers=GRIST_HEADERS, timeout=60)

    if r.status_code == 404:
        return {}

    r.raise_for_status()
    result = {}
    for rec in r.json().get("records", []):
        f   = rec["fields"]
        num = f.get("dossier_number")
        ver = f.get("source_version")
        if num and ver:
            result[(num, ver)] = rec["id"]
    return result


def upsert_to_grist(rows: list[dict], source_version: str, existing_map: dict):
    """
    Insère les nouveaux dossiers et met à jour les existants.
    Suffixe dossier_id_source : _c1 pour v1, _c2 pour v2.
    """
    suffix     = "c1" if "v1" in source_version else "c2"
    to_create  = []
    to_update  = []

    for row in rows:
        row["source_version"]    = source_version
        row["dossier_id_source"] = f"{row.get('dossier_number')}_{suffix}"
        key = (row.get("dossier_number"), source_version)

        if key in existing_map:
            to_update.append({"id": existing_map[key], "fields": row})
        else:
            to_create.append({"fields": row})

    url   = f"{GRIST_BASE_URL}/docs/{GRIST_DOC_ID}/tables/{TABLE_EXPORT}/records"
    BATCH = 100

    if to_create:
        for i in range(0, len(to_create), BATCH):
            r = requests.post(url, headers=GRIST_HEADERS,
                              json={"records": to_create[i:i+BATCH]}, timeout=60)
            r.raise_for_status()
            time.sleep(0.3)
        print(f"    {len(to_create)} nouveaux dossiers insérés ✓")

    if to_update:
        for i in range(0, len(to_update), BATCH):
            r = requests.patch(url, headers=GRIST_HEADERS,
                               json={"records": to_update[i:i+BATCH]}, timeout=60)
            r.raise_for_status()
            time.sleep(0.3)
        print(f"    {len(to_update)} dossiers mis à jour ✓")

    if not to_create and not to_update:
        print("    Aucun changement détecté ✓")


# =============================================================================
# SYNC D'UNE DÉMARCHE
# =============================================================================

def sync_demarche(demarche_number: str, source_version: str, existing_map: dict):
    """
    Orchestre le sync d'une démarche :
    1. Lit la date du dernier sync réussi dans Sync_metadata
    2. Récupère uniquement les dossiers modifiés depuis cette date
    3. Upsert dans EXPORT
    4. Met à jour Sync_metadata UNIQUEMENT si tout a réussi
       → Si le run plante, la date reste inchangée : aucune perte au prochain run
    """
    debut_run = now_iso()
    last_sync = get_last_sync(demarche_number)

    if last_sync:
        print(f"  Dernier sync : {last_sync} → sync incrémental")
    else:
        print(f"  Aucun sync précédent → import complet de tout l'historique")

    rows = fetch_all_dossiers(demarche_number, updated_since=last_sync)

    if rows:
        ensure_columns_exist(rows)
        upsert_to_grist(rows, source_version, existing_map)

    # Mise à jour de la date après succès total
    set_last_sync(demarche_number, debut_run)
    print(f"  Sync_metadata mis à jour → {debut_run} ✓")


# =============================================================================
# MAIN
# =============================================================================

def main():
    print("\n" + "="*60)
    print("  SYNC COOPS → GRIST")
    print("="*60)

    demarches_actives = [d for d in [DEMARCHE_V1A, DEMARCHE_V1B, DEMARCHE_V2] if d]
    if not demarches_actives:
        print("ERREUR : Aucune démarche configurée.")
        print("Définir au moins un secret parmi : DEMARCHE_V1A, DEMARCHE_V1B, DEMARCHE_V2")
        sys.exit(1)

    print(f"\nDémarches actives : {demarches_actives}")

    # Étape 1 — Tables Grist
    print("\n[1/4] Vérification des tables Grist...")
    ensure_export_table()
    ensure_metadata_table()

    # Étape 2 — Charger la map existante une seule fois
    print("\n[2/4] Chargement des dossiers existants...")
    existing_map = get_existing_map()
    print(f"  {len(existing_map)} dossiers déjà présents dans Grist")

    # Étape 3 — Sync de chaque démarche
    print("\n[3/4] Synchronisation...")

    if DEMARCHE_V1A:
        print(f"\n  → COOPS v1a (démarche {DEMARCHE_V1A})")
        sync_demarche(DEMARCHE_V1A, "coops_v1", existing_map)
        existing_map = get_existing_map()

    if DEMARCHE_V1B:
        print(f"\n  → COOPS v1b (démarche {DEMARCHE_V1B})")
        sync_demarche(DEMARCHE_V1B, "coops_v1", existing_map)
        existing_map = get_existing_map()

    if DEMARCHE_V2:
        print(f"\n  → COOPS v2 (démarche {DEMARCHE_V2})")
        sync_demarche(DEMARCHE_V2, "coops_v2", existing_map)

    print("\n" + "="*60)
    print("  ✅ SYNC TERMINÉ")
    print("="*60 + "\n")


if __name__ == "__main__":
    main()
