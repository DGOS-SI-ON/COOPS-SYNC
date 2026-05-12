# COOPS → Grist sync

Synchronise automatiquement les dossiers COOPS v1 et v2 depuis Démarches Simplifiées vers une table Grist unifiée.

- ✅ Aucun serveur requis
- ✅ Aucune base de données externe
- ✅ Données privées (transitent uniquement entre DS et Grist)
- ✅ Gratuit (GitHub Actions)
- ✅ Automatique toutes les 30 minutes

---

## Structure du projet

```
├── sync_direct.py              ← script principal
├── .github/
│   └── workflows/
│       └── sync_coops.yml      ← automatisation GitHub Actions
└── README.md
```

---

## Installation

### 1. Forker ce repo sur GitHub

### 2. Configurer les secrets GitHub

Dans ton repo GitHub → **Settings → Secrets and variables → Actions → New repository secret**

| Nom du secret   | Valeur                                      | Obligatoire |
|-----------------|---------------------------------------------|-------------|
| `DS_TOKEN`      | Ton token API Démarches Simplifiées         | ✅ Oui      |
| `DEMARCHE_V1`   | Numéro de la démarche COOPS v1              | ⚠️ Au moins un des deux |
| `DEMARCHE_V2`   | Numéro de la démarche COOPS v2              | ⚠️ Au moins un des deux |
| `GRIST_API_KEY` | Ta clé API Grist                            | ✅ Oui      |
| `GRIST_DOC_ID`  | L'ID de ton document Grist                  | ✅ Oui      |

#### Où trouver ces valeurs ?

**DS_TOKEN**
→ Démarches Simplifiées → ton profil → "Jeton d'API"

**DEMARCHE_V1 / DEMARCHE_V2**
→ L'URL de ta démarche : `demarches-simplifiees.fr/admin/procedures/XXXXXX`
→ Le numéro est le `XXXXXX`

**GRIST_API_KEY**
→ Grist → ton profil (en haut à droite) → "Clé API"

**GRIST_DOC_ID**
→ L'URL de ton document Grist : `grist.numerique.gouv.fr/doc/XXXXXX`
→ L'ID est le `XXXXXX`

---

### 3. Activer GitHub Actions

Dans ton repo → onglet **Actions** → cliquer sur "I understand my workflows, go ahead and enable them"

---

### 4. Lancer un premier sync manuellement

→ **Actions → Sync COOPS → Grist → Run workflow**

Le script va :
1. Créer automatiquement la table `EXPORT` dans Grist si elle n'existe pas
2. Créer toutes les colonnes nécessaires (v1 + v2 fusionnées)
3. Importer tous les dossiers existants

Ensuite il tournera **automatiquement toutes les 30 minutes**.

---

## Table EXPORT dans Grist

La table créée contient toutes les colonnes de v1 et v2 fusionnées.

| Colonne          | Description                                  |
|------------------|----------------------------------------------|
| `dossier_number` | Numéro unique du dossier DS                  |
| `source_version` | `coops_v1` ou `coops_v2`                     |
| `statut`         | État du dossier (en_construction, accepte…)  |
| `date_depot`     | Date de dépôt du dossier                     |
| `email_usager`   | Email de la personne qui a rempli le dossier |
| `...`            | Tous les champs du formulaire DS             |

Les colonnes présentes dans v1 mais pas v2 seront **vides** pour les dossiers v2, et inversement.

---

## Transition v1 → v2

Quand COOPS v1 sera arrêté :
1. Aller dans **Settings → Secrets → `DEMARCHE_V1`**
2. Supprimer ce secret (ou le laisser vide)
3. Le script ignorera automatiquement v1 et ne synchronisera plus que v2

Les données historiques de v1 resteront dans Grist.

---

## Fréquence de synchronisation

Par défaut : toutes les 30 minutes.

Pour changer, modifier dans `.github/workflows/sync_coops.yml` :
```yaml
- cron: '*/30 * * * *'   # toutes les 30 min
- cron: '0 * * * *'      # toutes les heures
- cron: '0 8 * * *'      # tous les jours à 8h
```
