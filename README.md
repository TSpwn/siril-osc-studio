# Siril OSC Studio

**Version 0.1.0 — work in progress 🚧**

Automated post-processing scripts and a small GUI app for **one-shot-color (OSC)**
astrophotography in [Siril](https://siril.org) 1.4.x. Takes calibrated frames all
the way from raw sub-exposures to a finished, stretched 16-bit TIFF — the stock
scripts stop at the linear `result.fit`, this goes the rest of the way.

*Scripts de post-traitement automatisé et une petite app graphique pour
l'astrophoto **couleur (OSC)** dans Siril 1.4.x. Va des brutes calibrées jusqu'au
TIFF 16-bit étiré et fini — les scripts d'origine s'arrêtent à `result.fit`
linéaire, celui-ci fait le reste.*

> 🇬🇧 English below · 🇫🇷 [Version française plus bas](#-français)

---

## 🇬🇧 English

### What's included

| File | What it does |
|------|--------------|
| `OSC_Full_Color.ssf` | Broadband color pipeline: calibration → registration → stacking → gradient removal → photometric color calibration (SPCC) → green removal → auto-stretch → 16-bit TIFF. |
| `OSC_Nebula_HaOIII.ssf` | Narrowband dual-band pipeline: extracts Ha and OIII, stacks each, composes an **HOO** image (R=Ha, G=OIII, B=OIII), then post-processes it. |
| `OSC_Studio.py` | All-in-one GUI (sirilpy + PyQt6): pick the target type, pick the folder, tick the optional steps, run. Live check of the required subfolders. |

Every run produces **two files** in the target folder:
- `result.fit` — the raw **linear** stack (to reprocess by hand),
- `result_processed.tif` — the processed **16-bit TIFF**.

### Requirements

- **Siril 1.4.x** (tested on 1.4.4), macOS or Windows.
- Your frames organised in four subfolders inside the target folder:
  `lights/`, `darks/`, `flats/`, `biases/`.
- The GUI needs PyQt6 — it is installed automatically on first launch via Siril's
  bundled Python (`ensure_installed`).

### Install

1. Copy the three files into a folder of your own (e.g. `Documents/Siril-scripts`).
   Do **not** put them in Siril's built-in scripts folder.
2. In Siril: **☰ → Preferences → Scripts**, add that folder, click **Refresh**, **Apply**.
3. Reload the menu: type `reloadscripts` in the command line (or restart Siril).
4. The scripts appear under **Scripts → Siril script files** (`.ssf`) and
   **Scripts → Python scripts** (`OSC_Studio`).

### Use

- **GUI (recommended):** *Scripts → Python scripts → OSC_Studio*. Choose the
  target type, browse to the target folder, tick/untick steps, **Run**.
- **Batch (`.ssf`):** set Siril's working directory to the target folder, then
  click the script. It starts immediately and needs the four subfolders.

### One-time SPCC setup (for correct colors)

Open the **SPCC** tool once in the Siril GUI, set your **sensor** (e.g. Canon R8),
your **filter** and the **white reference**, and run it once. The color pipeline
reuses those settings afterwards (`spcc` runs with no argument).

### Notes / design choices

- The order matters: **SPCC runs on linear data, before the stretch**. The final
  stretch uses `autostretch -linked` — the unlinked version would undo the white
  balance set by SPCC.
- `subsky 1` (degree-1 plane) is used for gradient removal — robust, since flats
  already handle vignetting.
- The narrowband pipeline deliberately **skips SPCC**: photometric calibration is
  not meaningful on a synthetic HOO composite; channel balancing is done by the
  OIII→Ha renormalisation instead.

### Roadmap (0.1 → next)

- Per-step progress bar.
- "How to organise my photos" helper with a folder diagram.
- Optional `spcc -narrowband` variant for dual-band targets.
- More pipelines (SHO, mono, …) — the app is built around a list of `Pipeline`
  objects precisely so it can grow.

### License

MIT — see [LICENSE](LICENSE).

---

## 🇫🇷 Français

### Contenu

| Fichier | Rôle |
|---------|------|
| `OSC_Full_Color.ssf` | Pipeline couleur large bande : calibration → alignement → empilement → retrait du gradient → calibration couleur photométrique (SPCC) → retrait du vert → étirement auto → TIFF 16-bit. |
| `OSC_Nebula_HaOIII.ssf` | Pipeline bande étroite dual-band : extrait Ha et OIII, empile chaque couche, compose une image **HOO** (R=Ha, G=OIII, B=OIII), puis post-traite. |
| `OSC_Studio.py` | App tout-en-un (sirilpy + PyQt6) : choisir le type de cible, le dossier, cocher les étapes, lancer. Vérification en direct des sous-dossiers requis. |

Chaque exécution produit **deux fichiers** dans le dossier de la cible :
- `result.fit` — l'empilement **linéaire** brut (à retraiter à la main),
- `result_processed.tif` — le **TIFF 16-bit** traité.

### Prérequis

- **Siril 1.4.x** (testé sur 1.4.4), macOS ou Windows.
- Tes images rangées dans quatre sous-dossiers du dossier de la cible :
  `lights/`, `darks/`, `flats/`, `biases/`.
- L'app a besoin de PyQt6 — installé automatiquement au premier lancement via le
  Python embarqué de Siril (`ensure_installed`).

### Installation

1. Copie les trois fichiers dans un dossier à toi (ex. `Documents/Siril-scripts`).
   Ne les mets **pas** dans le dossier des scripts intégrés de Siril.
2. Dans Siril : **☰ → Préférences → Scripts**, ajoute ce dossier, clique
   **Refresh**, puis **Apply**.
3. Recharge le menu : tape `reloadscripts` dans la ligne de commande (ou redémarre Siril).
4. Les scripts apparaissent sous **Scripts → Fichiers de Scripts Siril** (`.ssf`)
   et **Scripts → Scripts Python** (`OSC_Studio`).

### Utilisation

- **App (recommandé) :** *Scripts → Scripts Python → OSC_Studio*. Choisis le type
  de cible, sélectionne le dossier, coche/décoche les étapes, **Lancer**.
- **Batch (`.ssf`) :** règle le répertoire de travail de Siril sur le dossier de la
  cible, puis clique le script. Il démarre tout de suite et exige les quatre
  sous-dossiers.

### Réglage SPCC (une seule fois, pour des couleurs justes)

Ouvre l'outil **SPCC** une fois dans l'interface de Siril, choisis ton **capteur**
(ex. Canon R8), ton **filtre** et la **référence de blanc**, et lance-le une fois.
Le pipeline couleur réutilise ces réglages ensuite (`spcc` sans argument).

### Notes / choix de conception

- L'ordre est critique : **SPCC tourne sur des données linéaires, avant l'étirement**.
  L'étirement final utilise `autostretch -linked` — la version « unlinked »
  défairait la balance des blancs posée par SPCC.
- `subsky 1` (plan de degré 1) pour le gradient — robuste, car les flats
  corrigent déjà le vignettage.
- Le pipeline bande étroite **saute volontairement SPCC** : la calibration
  photométrique n'a pas de sens sur un composite HOO synthétique ; l'équilibrage
  des canaux est fait par la renormalisation OIII→Ha à la place.

### Feuille de route (0.1 → suite)

- Barre de progression par étape.
- Aide « comment ranger mes photos » avec un schéma des dossiers.
- Variante `spcc -narrowband` optionnelle pour les cibles dual-band.
- D'autres pipelines (SHO, mono, …) — l'app est bâtie autour d'une liste d'objets
  `Pipeline` justement pour pouvoir grandir.

### Licence

MIT — voir [LICENSE](LICENSE).
