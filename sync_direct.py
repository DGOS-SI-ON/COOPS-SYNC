"""
sync_direct.py
--------------
Synchronise les dossiers COOPS v1 et v2 depuis Démarches Simplifiées vers Grist.
- Aucun serveur requis
- Aucune base de données externe
- Grist est utilisé comme source de vérité pour éviter les doublons
- Les secrets sont lus depuis les variables d'environnement (GitHub Secrets)

Dépendances : pip install requests
"""

import os
import sys
import time
import requests

# =============================================================================
# CONFIGURATION (depuis les variables d'environnement / GitHub Secrets)
# =============================================================================

DS_TOKEN       = os.environ["DS_TOKEN"]
DEMARCHE_V1    = os.environ.get("DEMARCHE_V1", "")   # Laisser vide si pas encore dispo
DEMARCHE_V2    = os.environ.get("DEMARCHE_V2", "")   # Laisser vide si pas encore dispo
GRIST_API_KEY  = os.environ["GRIST_API_KEY"]
GRIST_DOC_ID   = os.environ["GRIST_DOC_ID"]
GRIST_BASE_URL = os.environ.get("GRIST_BASE_URL", "https://grist.numerique.gouv.fr/api")
TABLE_EXPORT   = os.environ.get("GRIST_TABLE", "EXPORT")

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
# REQUÊTE GRAPHQL — récupère tous les dossiers d'une démarche avec pagination
# =============================================================================

QUERY = """
query getDossiers($demarcheNumber: Int!, $after: String) {
  demarche(number: $demarcheNumber) {
    dossiers(first: 100, after: $after) {
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
            raisonSociale
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
# FONCTIONS DS
# =============================================================================

def fetch_all_dossiers(demarche_number: str) -> list[dict]:
    """
    Récupère tous les dossiers d'une démarche DS via GraphQL avec pagination.
    Retourne une liste de dossiers normalisés (dict plat).
    """
    print(f"  Récupération des dossiers de la démarche {demarche_number}...")
    all_dossiers = []
    cursor = None
    page = 1

    while True:
        variables = {"demarcheNumber": int(demarche_number), "after": cursor}
        response = requests.post(
            DS_API_URL,
            headers=DS_HEADERS,
            json={"query": QUERY, "variables": variables},
            timeout=60,
        )
        response.raise_for_status()
        data = response.json()

        if "errors" in data:
            print(f"  ERREUR GraphQL : {data['errors']}", file=sys.stderr)
            break

        dossiers_data = data["data"]["demarche"]["dossiers"]
        nodes = dossiers_data["nodes"]
        page_info = dossiers_data["pageInfo"]

        for node in nodes:
            all_dossiers.append(normalize_dossier(node))

        print(f"    Page {page} : {len(nodes)} dossiers récupérés")

        if not page_info["hasNextPage"]:
            break

        cursor = page_info["endCursor"]
        page += 1
        time.sleep(0.5)  # Respecter le rate limit DS

    print(f"  Total : {len(all_dossiers)} dossiers")
    return all_dossiers


def normalize_dossier(node: dict) -> dict:
    """
    Transforme un nœud GraphQL DS en dict plat pour Grist.
    Les champs répétables sont aplatis avec un index (ex: piece_jointe_1, piece_jointe_2).
    """
    row = {}

    # Métadonnées du dossier
    row["dossier_number"]                  = node.get("number")
    row["statut"]                          = node.get("state", "")
    row["date_depot"]                      = node.get("dateDepot", "") or ""
    row["date_passage_en_construction"]    = node.get("datePassageEnConstruction", "") or ""
    row["date_passage_en_instruction"]     = node.get("datePassageEnInstruction", "") or ""
    row["date_traitement"]                 = node.get("dateTraitement", "") or ""
    row["motivation"]                      = node.get("motivation", "") or ""
    row["email_usager"]                    = (node.get("usager") or {}).get("email", "") or ""
    row["groupe_instructeur"]              = (node.get("groupeInstructeur") or {}).get("label", "") or ""

    # Demandeur (personne physique ou morale)
    demandeur = node.get("demandeur") or {}
    row["demandeur_civilite"]     = demandeur.get("civilite", "") or ""
    row["demandeur_nom"]          = demandeur.get("nom", "") or ""
    row["demandeur_prenom"]       = demandeur.get("prenom", "") or ""
    row["demandeur_email"]        = demandeur.get("email", "") or ""
    row["demandeur_siret"]        = demandeur.get("siret", "") or ""
    row["demandeur_raison_sociale"]= demandeur.get("raisonSociale", "") or ""

    # Champs du formulaire → colonnes dynamiques
    for champ in node.get("champs", []) or []:
        col_name = label_to_column(champ.get("label", ""))
        if not col_name:
            continue

        # Champ répétable → on aplatit les lignes
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


def label_to_column(label: str) -> str:
    """
    Convertit un label DS en nom de colonne Grist valide.
    Ex: "Nom du médecin" → "nom_du_medecin"
    """
    import unicodedata
    import re
    label = label.lower().strip()
    label = unicodedata.normalize("NFD", label)
    label = "".join(c for c in label if unicodedata.category(c) != "Mn")
    label = re.sub(r"[^a-z0-9]+", "_", label)
    label = label.strip("_")
    return label[:60]  # Limite raisonnable pour Grist


# =============================================================================
# FONCTIONS GRIST
# =============================================================================

def get_existing_dossier_numbers() -> dict[int, int]:
    """
    Retourne un dict {dossier_number: grist_row_id} pour tous les enregistrements
    déjà présents dans la table EXPORT. Utilisé pour l'upsert.
    """
    url = f"{GRIST_BASE_URL}/docs/{GRIST_DOC_ID}/tables/{TABLE_EXPORT}/records"
    r = requests.get(url, headers=GRIST_HEADERS, timeout=30)

    if r.status_code == 404:
        return {}  # Table pas encore créée

    r.raise_for_status()
    records = r.json().get("records", [])
    return {
        rec["fields"].get("dossier_number"): rec["id"]
        for rec in records
        if rec["fields"].get("dossier_number")
    }


def get_existing_columns() -> set[str]:
    """Retourne l'ensemble des colonnes existantes dans la table EXPORT."""
    url = f"{GRIST_BASE_URL}/docs/{GRIST_DOC_ID}/tables/{TABLE_EXPORT}/columns"
    r = requests.get(url, headers=GRIST_HEADERS, timeout=30)

    if r.status_code == 404:
        return set()

    r.raise_for_status()
    return {
        col["id"]
        for col in r.json().get("columns", [])
        if col["id"] not in ("id", "manualSort")
    }


def ensure_table_exists():
    """Crée la table EXPORT dans Grist si elle n'existe pas."""
    url = f"{GRIST_BASE_URL}/docs/{GRIST_DOC_ID}/tables"
    r = requests.get(url, headers=GRIST_HEADERS, timeout=30)
    r.raise_for_status()

    existing = [t["id"] for t in r.json().get("tables", [])]
    if TABLE_EXPORT in existing:
        print(f"  Table '{TABLE_EXPORT}' déjà existante ✓")
        return

    print(f"  Création de la table '{TABLE_EXPORT}'...")
    r = requests.post(url, headers=GRIST_HEADERS, json={
        "tables": [{
            "id": TABLE_EXPORT,
            "columns": [
                {"id": "dossier_number",  "fields": {"type": "Int",  "label": "N° Dossier"}},
                {"id": "source_version",  "fields": {"type": "Text", "label": "Version"}},
                {"id": "statut",          "fields": {"type": "Text", "label": "Statut"}},
                {"id": "date_depot",      "fields": {"type": "Text", "label": "Date de dépôt"}},
                {"id": "email_usager",    "fields": {"type": "Text", "label": "Email usager"}},
                {"id": "groupe_instructeur", "fields": {"type": "Text", "label": "Groupe instructeur"}},
            ]
        }]
    })
    r.raise_for_status()
    print(f"  Table '{TABLE_EXPORT}' créée ✓")


def ensure_columns_exist(all_rows: list[dict]):
    """
    Crée dans Grist toutes les colonnes présentes dans les données
    mais absentes de la table EXPORT.
    """
    existing_cols = get_existing_columns()

    needed_cols = set()
    for row in all_rows:
        needed_cols.update(row.keys())
    needed_cols.add("source_version")

    missing = needed_cols - existing_cols
    if not missing:
        print(f"  Colonnes à jour ({len(existing_cols)} colonnes) ✓")
        return

    print(f"  Ajout de {len(missing)} colonnes manquantes...")
    url = f"{GRIST_BASE_URL}/docs/{GRIST_DOC_ID}/tables/{TABLE_EXPORT}/columns"
    payload = [{"id": col, "fields": {"type": "Text"}} for col in sorted(missing)]
    r = requests.post(url, headers=GRIST_HEADERS, json={"columns": payload}, timeout=30)
    r.raise_for_status()
    print(f"  Colonnes ajoutées : {sorted(missing)[:5]}{'...' if len(missing) > 5 else ''} ✓")


def upsert_to_grist(rows: list[dict], source_version: str, existing_map: dict):
    """
    Insère les nouveaux dossiers et met à jour les existants dans Grist.
    Travaille par lots de 100 pour éviter les timeouts.
    """
    to_create = []
    to_update = []

    for row in rows:
        row["source_version"] = source_version
        dossier_number = row.get("dossier_number")

        if dossier_number in existing_map:
            to_update.append({
                "id": existing_map[dossier_number],
                "fields": row
            })
        else:
            to_create.append({"fields": row})

    url = f"{GRIST_BASE_URL}/docs/{GRIST_DOC_ID}/tables/{TABLE_EXPORT}/records"
    BATCH = 100

    if to_create:
        for i in range(0, len(to_create), BATCH):
            batch = to_create[i:i + BATCH]
            r = requests.post(url, headers=GRIST_HEADERS,
                              json={"records": batch}, timeout=60)
            r.raise_for_status()
            time.sleep(0.3)
        print(f"  {len(to_create)} nouveaux dossiers insérés ✓")

    if to_update:
        for i in range(0, len(to_update), BATCH):
            batch = to_update[i:i + BATCH]
            r = requests.patch(url, headers=GRIST_HEADERS,
                               json={"records": batch}, timeout=60)
            r.raise_for_status()
            time.sleep(0.3)
        print(f"  {len(to_update)} dossiers mis à jour ✓")

    if not to_create and not to_update:
        print("  Aucun changement détecté ✓")


# =============================================================================
# MAIN
# =============================================================================

def main():
    print("\n" + "="*60)
    print("  SYNC COOPS → GRIST")
    print("="*60)

    if not DEMARCHE_V1 and not DEMARCHE_V2:
        print("ERREUR : Aucune démarche configurée (DEMARCHE_V1 et DEMARCHE_V2 sont vides)")
        sys.exit(1)

    # 1. S'assurer que la table EXPORT existe dans Grist
    print("\n[1/5] Vérification de la table Grist...")
    ensure_table_exists()

    # 2. Récupérer les dossiers depuis DS
    all_rows = []

    if DEMARCHE_V1:
        print(f"\n[2/5] Récupération COOPS v1 (démarche {DEMARCHE_V1})...")
        rows_v1 = fetch_all_dossiers(DEMARCHE_V1)
        for row in rows_v1:
            row["source_version"] = "coops_v1"
        all_rows.extend(rows_v1)
    else:
        print("\n[2/5] COOPS v1 non configuré, ignoré.")
        rows_v1 = []

    if DEMARCHE_V2:
        print(f"\n[3/5] Récupération COOPS v2 (démarche {DEMARCHE_V2})...")
        rows_v2 = fetch_all_dossiers(DEMARCHE_V2)
        for row in rows_v2:
            row["source_version"] = "coops_v2"
        all_rows.extend(rows_v2)
    else:
        print("\n[3/5] COOPS v2 non configuré, ignoré.")
        rows_v2 = []

    if not all_rows:
        print("\nAucun dossier récupéré, fin du script.")
        return

    # 3. Créer les colonnes manquantes dans Grist
    print(f"\n[4/5] Mise à jour des colonnes ({len(all_rows)} dossiers, démarches fusionnées)...")
    ensure_columns_exist(all_rows)

    # 4. Récupérer les dossiers déjà dans Grist (pour l'upsert)
    print("\n[5/5] Synchronisation vers Grist...")
    existing_map = get_existing_dossier_numbers()
    print(f"  {len(existing_map)} dossiers déjà présents dans Grist")

    if DEMARCHE_V1 and rows_v1:
        print(f"\n  → COOPS v1 :")
        upsert_to_grist(rows_v1, "coops_v1", existing_map)
        # Mettre à jour la map pour v2 (évite les doublons si même numéro)
        existing_map = get_existing_dossier_numbers()

    if DEMARCHE_V2 and rows_v2:
        print(f"\n  → COOPS v2 :")
        upsert_to_grist(rows_v2, "coops_v2", existing_map)

    print("\n" + "="*60)
    print("  ✅ SYNC TERMINÉ")
    print("="*60 + "\n")


if __name__ == "__main__":
    main()
