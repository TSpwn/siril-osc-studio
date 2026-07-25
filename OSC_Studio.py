#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OSC Studio — mini-app de traitement Siril 1.4 (sirilpy + PyQt6)
===============================================================

Petite application « tout-en-un » pour caméra couleur (OSC), pensée pour
débuter facilement :

  1. On choisit le TYPE DE CIBLE (pipeline) :
        - Couleur (large bande)
        - Nébuleuse (bande étroite, filtre dual-band -> composite HOO)
  2. On choisit le DOSSIER de la cible (qui contient lights/darks/flats/biases).
     Les 4 sous-dossiers sont vérifiés en direct (vert = trouvé, rouge = manquant).
  3. On coche les étapes voulues, puis « Lancer le traitement ».

Sorties, dans le dossier de la cible :
  * result.fit            -> empilement linéaire brut (à retraiter à la main)
  * result_processed.tif  -> TIFF 16-bit traité

Architecture (volontairement extensible — cette app est censée grandir) :
  * un pipeline = un objet Pipeline (étapes fixes + étapes optionnelles) ;
  * une étape optionnelle = un objet Step (nom, commandes, activé) ;
  * ajouter un pipeline = ajouter un Pipeline dans PIPELINES ;
  * ajouter une étape = ajouter un Step dans la liste du pipeline.
    L'interface (menu déroulant, cases à cocher, aide) se met à jour toute seule.

Portabilité macOS / Windows :
  * aucun chemin ni séparateur écrit à la main : le dossier de travail est choisi
    par l'utilisateur et manipulé via pathlib, puis transmis à Siril en notation
    POSIX (as_posix()), acceptée sur les deux OS ;
  * les arguments des commandes Siril utilisent la notation portable de Siril
    ('../process', etc.), exactement comme les scripts .ssf ;
  * PyQt6 fonctionne à l'identique sur macOS et Windows.

L'API Python de Siril 1.4 est expérimentale : on reste sur des commandes stables
(identiques aux scripts .ssf), on gère proprement l'échec de connexion
(SirilConnectionError) et on journalise chaque commande.
"""

from dataclasses import dataclass, field
from pathlib import Path
import json
import re
import sys

# Réglages mémorisés entre deux lancements. Dans le dossier personnel (et non
# dans le dépôt) pour ne pas le polluer ; chemin construit via pathlib.
SETTINGS_PATH = Path.home() / ".osc_studio_settings.json"


def sanitize_name(raw, default="result"):
    """Nettoie un nom de sortie pour en faire un nom de fichier sûr et sans espace.

    Sans espace = pas besoin de guillemets, et le découpage des arguments Siril
    reste fiable. Retourne `default` si le résultat est vide.
    """
    raw = (raw or "").strip()
    if not raw:
        return default
    cleaned = re.sub(r"\s+", "_", raw)              # espaces -> underscore
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "", cleaned)  # ne garder que sûr
    # Exiger au moins un caractère alphanumérique : évite ".."/"." (remontée
    # d'arborescence) et les noms vides après nettoyage.
    if not re.search(r"[A-Za-z0-9]", cleaned):
        return default
    return cleaned

# --- Connexion à Siril -------------------------------------------------------
import sirilpy as s
from sirilpy import SirilConnectionError

# Installe PyQt6 dans le venv du script si nécessaire (protégé : si on est
# hors-ligne mais que PyQt6 est déjà là, l'import ci-dessous fonctionne quand même).
try:
    s.ensure_installed("PyQt6")
except Exception:
    pass

from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QCheckBox, QGroupBox, QComboBox, QTextEdit, QFileDialog,
    QMessageBox, QDialog, QTextBrowser, QProgressBar, QScrollArea, QFrame,
)
from PyQt6.QtCore import QUrl, QThread, pyqtSignal, Qt
from PyQt6.QtGui import QDesktopServices, QPixmap


# =============================================================================
#  DÉFINITION DES PIPELINES (données, pas de logique)
# =============================================================================

# Sous-dossiers attendus dans le dossier de la cible.
REQUIRED_DIRS = ("lights", "darks", "flats", "biases")

# Extensions comptées comme des images (brutes RAW ou FITS) dans les sous-dossiers.
# Le Canon R8 produit du .CR3 ; N.I.N.A. sauve généralement du .fits.
IMAGE_EXTS = {
    ".fit", ".fits", ".fts",                    # FITS
    ".cr3", ".cr2", ".nef", ".arw", ".dng",     # RAW (Canon R8 = CR3)
    ".raw", ".pef", ".orf", ".raf",             # autres RAW
    ".tif", ".tiff", ".png", ".jpg", ".jpeg",   # images matricielles
    ".xisf", ".ser",                            # formats astro
}

@dataclass
class Step:
    """Étape post-stack activable/désactivable (une case à cocher)."""
    key: str
    label: str
    commands: list = field(default_factory=list)   # liste de (commande, arguments)
    enabled: bool = True                            # état par défaut de la case


# --- Blocs de commandes fixes ------------------------------------------------
# Masters bias / flat / dark : identiques pour les deux pipelines.
# Repris tels quels de la logique éprouvée des scripts d'origine.
_MASTERS = [
    # -- Biais --
    ("cd", "biases"),
    ("convert", "bias -out=../process"),
    ("cd", "../process"),
    ("stack", "bias rej 3 3 -nonorm -out=../masters/bias_stacked"),
    ("cd", ".."),
    # -- Flats (calibrés par le master bias) --
    ("cd", "flats"),
    ("convert", "flat -out=../process"),
    ("cd", "../process"),
    ("calibrate", "flat -bias=../masters/bias_stacked"),
    ("stack", "pp_flat rej 3 3 -norm=mul -out=../masters/pp_flat_stacked"),
    ("cd", ".."),
    # -- Darks --
    ("cd", "darks"),
    ("convert", "dark -out=../process"),
    ("cd", "../process"),
    ("stack", "dark rej 3 3 -nonorm -out=../masters/dark_stacked"),
    ("cd", ".."),
]

# Pipeline COULEUR : calibration des lights avec débayerisation, empilement,
# puis sauvegarde de l'empilement linéaire result.fit.
_COLOR_LIGHTS = [
    ("cd", "lights"),
    ("convert", "light -out=../process"),
    ("cd", "../process"),
    ("calibrate",
     "light -dark=../masters/dark_stacked -flat=../masters/pp_flat_stacked "
     "-cc=dark -cfa -equalize_cfa -debayer"),
    ("register", "pp_light"),
    ("stack",
     "r_pp_light rej 3 3 -norm=addscale -output_norm -rgb_equal -32b -out=result"),
    # L'empilement linéaire reste chargé en mémoire ; sa sauvegarde (avec le nom
    # de sortie choisi) est faite par le worker, juste après ces étapes fixes.
    ("load", "result"),
    ("mirrorx", "-bottomup"),
]
COLOR_FIXED = _MASTERS + _COLOR_LIGHTS

# Pipeline NÉBULEUSE (bande étroite) : calibration SANS débayerisation
# (l'extraction Ha/OIII travaille sur les données CFA), extraction, empilement
# séparé Ha et OIII, alignement des deux, renormalisation OIII->Ha, puis
# composition HOO (R=Ha, G=OIII, B=OIII) sauvée en result.fit.
_NEBULA_LIGHTS = [
    ("cd", "lights"),
    ("convert", "light -out=../process"),
    ("cd", "../process"),
    ("calibrate",
     "light -dark=../masters/dark_stacked -flat=../masters/pp_flat_stacked "
     "-cc=dark -cfa -equalize_cfa"),
    ("seqextract_HaOIII", "pp_light -resample=ha"),
    # Ha
    ("register", "Ha_pp_light"),
    ("stack",
     "r_Ha_pp_light rej 3 3 -norm=addscale -output_norm -32b -out=results_00001"),
    ("mirrorx_single", "results_00001"),
    # OIII
    ("register", "OIII_pp_light"),
    ("stack",
     "r_OIII_pp_light rej 3 3 -norm=addscale -output_norm -32b -out=results_00002"),
    ("mirrorx_single", "results_00002"),
    # Alignement fin des deux couches
    ("register", "results -transf=shift -interp=none"),
    # Renormalisation OIII sur les statistiques de Ha (PixelMath).
    ("pm",
     "$r_results_00002$*mad($r_results_00001$)/mad($r_results_00002$)"
     "-mad($r_results_00001$)/mad($r_results_00002$)*median($r_results_00002$)"
     "+median($r_results_00001$)"),
    ("save", "OIII_renorm"),
    # Composition HOO : R=Ha, G=OIII, B=OIII -> empilement linéaire chargé en
    # mémoire ; sauvegarde faite par le worker avec le nom de sortie choisi.
    ("rgbcomp", "r_results_00001 OIII_renorm OIII_renorm -out=result"),
    ("load", "result"),
]
NEBULA_FIXED = _MASTERS + _NEBULA_LIGHTS


def _color_steps():
    """Étapes optionnelles du pipeline couleur (ordre critique : SPCC avant l'étirement)."""
    return [
        Step("gradient", "Retrait du gradient / fond de ciel  (subsky)",
             [("subsky", "1")]),
        Step("color", "Calibration couleur photométrique  (platesolve + spcc)",
             # SPCC exige un plate-solve préalable en script ; 'spcc' sans
             # argument réutilise les réglages du dernier usage de l'outil SPCC.
             [("platesolve", ""), ("spcc", "")]),
        Step("green", "Suppression de la dominante verte  (rmgreen)",
             [("rmgreen", "")]),
        Step("stretch", "Étirement automatique -> non linéaire  (autostretch -linked)",
             # -linked OBLIGATOIRE après calibration couleur (préserve la balance
             # des blancs fixée par SPCC).
             [("autostretch", "-linked")]),
    ]


def _nebula_steps():
    """Étapes optionnelles du pipeline nébuleuse.

    Pas de SPCC ici : la calibration photométrique n'a pas de sens sur un
    composite HOO synthétique ; l'équilibrage des canaux est déjà fait par la
    renormalisation OIII->Ha (étape fixe). Voir l'aide.
    """
    return [
        Step("gradient", "Retrait du gradient / fond de ciel  (subsky)",
             [("subsky", "1")]),
        Step("green", "Suppression de la dominante verte  (rmgreen)",
             # Sur du HOO, nettoie les étoiles vertes mais décale l'OIII vers le
             # bleu ; décocher pour garder l'OIII turquoise.
             [("rmgreen", "")]),
        Step("stretch", "Étirement automatique -> non linéaire  (autostretch -linked)",
             [("autostretch", "-linked")]),
    ]


@dataclass
class Pipeline:
    """Un pipeline complet sélectionnable dans le menu déroulant."""
    key: str
    label: str            # texte affiché dans le menu déroulant
    description: str      # texte affiché dans l'aide
    fixed: list           # étapes fixes : calibration ... -> empilement linéaire
    post_steps: list      # étapes optionnelles (cases à cocher)


# Liste des pipelines. Pour en ajouter un : ajouter une entrée ici.
PIPELINES = [
    Pipeline(
        "color",
        "Couleur — cible large bande",
        "Cible couleur classique (galaxies, amas, nébuleuses en RVB). "
        "Débayerise, empile, puis calibre les couleurs (SPCC) avant d'étirer.",
        COLOR_FIXED,
        _color_steps(),
    ),
    Pipeline(
        "nebula",
        "Nébuleuse — bande étroite dual-band (HOO)",
        "Cible en bande étroite avec filtre dual-band. Extrait les couches Ha et "
        "OIII, les empile séparément, compose une image HOO (R=Ha, G=OIII, B=OIII) "
        "puis l'étire.",
        NEBULA_FIXED,
        _nebula_steps(),
    ),
]


# =============================================================================
#  THREAD DE TRAITEMENT
# =============================================================================

class ProcessingWorker(QThread):
    """Exécute le pipeline dans un thread séparé pour ne pas figer la fenêtre.

    Règle de sécurité (API sirilpy expérimentale) : SEUL ce thread touche la
    connexion Siril. Le thread principal ne fait que réagir aux signaux ci-dessous
    pour mettre à jour l'affichage — il n'appelle jamais siril.* pendant un run.
    """

    sig_log = pyqtSignal(str)          # ligne à ajouter au journal (GUI seulement)
    sig_progress = pyqtSignal(int, int)  # (nombre d'étapes faites, total)
    sig_phase = pyqtSignal(str)        # libellé de l'étape en cours
    sig_done = pyqtSignal(bool, str)   # (succès, message d'erreur éventuel)

    def __init__(self, siril, jobs, pipeline, enabled_flags):
        super().__init__()
        self.siril = siril
        self.jobs = jobs                     # liste de (dossier cible: Path, nom: str)
        self.pipeline = pipeline
        self.enabled_flags = enabled_flags   # bool par étape optionnelle
        self._stop = False                   # demande d'arrêt coopératif

    def request_stop(self):
        """Demande l'arrêt : il prend effet à la fin de la commande en cours."""
        self._stop = True

    def _build_program(self):
        """Construit la liste plate (phase, commande, arguments) pour TOUTES les cibles.

        Chaque cible démarre par un `cd` absolu vers son dossier, ce qui rend les
        cibles indépendantes (pas de suivi de position relative entre elles).
        """
        phase_fixed = f"Calibration / alignement / empilement — {self.pipeline.label}"
        multi = len(self.jobs) > 1
        prog = []
        for work, name in self.jobs:
            tag = f"[{name}] " if multi else ""   # préfixe de cible en mode lot
            prog.append((tag + "Préparation", "cd", f'"{work.as_posix()}"'))
            # Étapes fixes -> empilement linéaire chargé en mémoire.
            for command, argstring in self.pipeline.fixed:
                prog.append((tag + phase_fixed, command, argstring))
            # Image LINÉAIRE brute : <nom>.fit dans le dossier cible.
            prog.append((tag + f"Sauvegarde de l'image linéaire ({name}.fit)",
                         "save", f"../{name}"))
            # Étapes optionnelles cochées.
            for step, enabled in zip(self.pipeline.post_steps, self.enabled_flags):
                if enabled:
                    for command, argstring in step.commands:
                        prog.append((tag + step.label, command, argstring))
            # TIFF 16-bit final : <nom>_processed.tif.
            prog.append((tag + f"Sauvegarde du TIFF ({name}_processed.tif)",
                         "savetif", f"../{name}_processed"))
            # PNG d'aperçu (Qt le lit nativement pour la vignette).
            prog.append((tag + "Aperçu", "savepng", f"../{name}_preview"))
            prog += [(tag + "Finalisation", "cd", ".."),
                     (tag + "Finalisation", "close", "")]
        return prog

    def _emit_log(self, msg):
        """Journalise dans la fenêtre (signal) ET dans la console Siril.

        Les deux appels partent du thread worker : siril.log() et siril.cmd()
        sont ainsi toujours sérialisés sur le même thread, jamais concurrents.
        """
        self.sig_log.emit(msg)
        try:
            self.siril.log(msg)
        except Exception:
            pass

    def run(self):
        program = self._build_program()
        total = len(program)
        self.sig_progress.emit(0, total)
        try:
            for i, (phase, command, argstring) in enumerate(program):
                # Arrêt coopératif : vérifié entre deux commandes.
                if self._stop:
                    self.sig_done.emit(False, "Traitement arrêté par l'utilisateur.")
                    return
                self.sig_phase.emit(phase)
                tokens = argstring.split() if argstring else []
                self._emit_log("> " + " ".join([command, *tokens]).strip())
                self.siril.cmd(command, *tokens)   # appel Siril (bloquant)
                self.sig_progress.emit(i + 1, total)
            self.sig_done.emit(True, "")
        except SirilConnectionError as e:
            self.sig_done.emit(False, f"Connexion perdue : {e}")
        except Exception as e:
            self.sig_done.emit(False, str(e))


# =============================================================================
#  INTERFACE GRAPHIQUE
# =============================================================================

class OscStudioWindow(QWidget):

    def __init__(self):
        super().__init__()
        self.setObjectName("window")
        self.setWindowTitle("OSC Studio — Siril")
        self.setMinimumSize(720, 760)

        self.siril = None
        self.connected = False
        self.checkboxes = []          # cases à cocher du pipeline courant
        self.status_labels = {}       # étiquettes ✓/✗ par sous-dossier
        self._worker = None           # thread de traitement (None = inactif)

        self._build_ui()
        self.setStyleSheet(self._stylesheet())   # thème sombre « astro »
        self._connect_to_siril()
        self._rebuild_steps()          # remplit les cases du pipeline par défaut
        self._apply_settings(self._load_settings())   # restaure les réglages mémorisés
        self._update_folder_status()   # état initial (bouton désactivé)

    # ---- Construction de l'interface ----------------------------------------
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ================= Barre d'en-tête (fixe, hors défilement) =============
        header = QWidget()
        header.setObjectName("header")
        hlay = QHBoxLayout(header)
        hlay.setContentsMargins(22, 16, 22, 16)
        title_box = QVBoxLayout()
        title_box.setSpacing(1)
        title = QLabel("🔭  OSC Studio")
        title.setObjectName("title")
        subtitle = QLabel("Traitement guidé pour caméra couleur — Siril 1.4")
        subtitle.setObjectName("subtitle")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        hlay.addLayout(title_box)
        hlay.addStretch(1)
        self.conn_lbl = QLabel("●  Connexion…")
        self.conn_lbl.setObjectName("conn")
        hlay.addWidget(self.conn_lbl)
        about_btn = QPushButton("Aide")
        about_btn.setObjectName("ghost")
        about_btn.clicked.connect(self.show_about)
        hlay.addWidget(about_btn)
        root.addWidget(header)

        # ================= Contenu défilant ====================================
        scroll = QScrollArea()
        scroll.setObjectName("scroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        content.setObjectName("content")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(20, 18, 20, 20)
        layout.setSpacing(14)

        intro = QLabel("Trois étapes : le type de cible, le dossier des photos, "
                       "puis « Lancer ». C'est tout.")
        intro.setObjectName("help")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        # ----------------- ①  Type de cible ------------------------------------
        pipe_box = QGroupBox("①   Type de cible")
        pipe_lay = QVBoxLayout(pipe_box)
        pipe_lay.setSpacing(8)
        self.pipeline_combo = QComboBox()
        for p in PIPELINES:
            self.pipeline_combo.addItem(p.label)
        # Connecté APRÈS addItem pour ne pas déclencher _rebuild_steps trop tôt.
        self.pipeline_combo.currentIndexChanged.connect(self._rebuild_steps)
        pipe_lay.addWidget(self.pipeline_combo)
        self.pipe_desc = QLabel("")
        self.pipe_desc.setObjectName("sectionHint")
        self.pipe_desc.setWordWrap(True)
        pipe_lay.addWidget(self.pipe_desc)
        layout.addWidget(pipe_box)

        # ----------------- ②  Dossier des photos -------------------------------
        dir_box = QGroupBox("②   Dossier des photos")
        dir_lay = QVBoxLayout(dir_box)
        dir_lay.setSpacing(8)
        self.dir_hint = QLabel("Le dossier doit contenir les sous-dossiers "
                               "lights, darks, flats et biases.")
        self.dir_hint.setObjectName("sectionHint")
        self.dir_hint.setWordWrap(True)
        dir_lay.addWidget(self.dir_hint)

        row = QHBoxLayout()
        self.dir_edit = QLineEdit()
        self.dir_edit.setPlaceholderText("Aucun dossier choisi…")
        self.dir_edit.textChanged.connect(self._update_folder_status)
        row.addWidget(self.dir_edit, 1)
        self.browse_btn = QPushButton("Parcourir…")
        self.browse_btn.setObjectName("ghost")
        self.browse_btn.clicked.connect(self._choose_dir)
        row.addWidget(self.browse_btn)
        dir_lay.addLayout(row)

        self.batch_check = QCheckBox(
            "Mode lot : ce dossier contient plusieurs cibles (un sous-dossier par cible)")
        self.batch_check.toggled.connect(self._on_batch_toggled)
        dir_lay.addWidget(self.batch_check)

        # Vérif en direct — mode normal : les 4 voyants avec leur nombre d'images.
        self.single_status_widget = QWidget()
        srow = QHBoxLayout(self.single_status_widget)
        srow.setContentsMargins(0, 2, 0, 0)
        srow.setSpacing(16)
        for name in REQUIRED_DIRS:
            lbl = QLabel(f"✗  {name}")
            self.status_labels[name] = lbl
            srow.addWidget(lbl)
        srow.addStretch(1)
        dir_lay.addWidget(self.single_status_widget)
        # Vérif en direct — mode lot : nombre de cibles valides trouvées.
        self.batch_status_widget = QWidget()
        brow = QHBoxLayout(self.batch_status_widget)
        brow.setContentsMargins(0, 2, 0, 0)
        self.batch_status_lbl = QLabel("")
        self.batch_status_lbl.setWordWrap(True)
        brow.addWidget(self.batch_status_lbl, 1)
        dir_lay.addWidget(self.batch_status_widget)
        self.batch_status_widget.setVisible(False)   # caché hors mode lot
        layout.addWidget(dir_box)

        # ----------------- ③  Options du traitement ----------------------------
        self.steps_box = QGroupBox("③   Options du traitement")
        opt_lay = QVBoxLayout(self.steps_box)
        opt_lay.setSpacing(10)

        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Nom de sortie :"))
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("result  (ex. M31, NGC7000 — vide = result)")
        name_row.addWidget(self.name_edit, 1)
        opt_lay.addLayout(name_row)

        steps_hint = QLabel("Étapes appliquées après l'empilement — décoche pour en sauter :")
        steps_hint.setObjectName("sectionHint")
        steps_hint.setWordWrap(True)
        opt_lay.addWidget(steps_hint)

        # Conteneur des cases à cocher (rempli/vidé par _rebuild_steps).
        self.steps_layout = QVBoxLayout()
        self.steps_layout.setSpacing(4)
        opt_lay.addLayout(self.steps_layout)
        layout.addWidget(self.steps_box)

        # ----------------- Lancer / Arrêter ------------------------------------
        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        self.run_btn = QPushButton("🚀   Lancer le traitement")
        self.run_btn.setObjectName("primary")
        self.run_btn.clicked.connect(self.on_run)
        btn_row.addWidget(self.run_btn, 1)
        self.stop_btn = QPushButton("Arrêter")
        self.stop_btn.setObjectName("danger")
        self.stop_btn.clicked.connect(self.on_stop)
        self.stop_btn.setEnabled(False)   # actif seulement pendant un traitement
        btn_row.addWidget(self.stop_btn)
        layout.addLayout(btn_row)

        # ----------------- Progression -----------------------------------------
        self.phase_lbl = QLabel("")       # ex. « Empilement… »
        self.phase_lbl.setObjectName("sectionHint")
        self.phase_lbl.setWordWrap(True)
        layout.addWidget(self.phase_lbl)
        self.progress = QProgressBar()
        self.progress.setValue(0)
        layout.addWidget(self.progress)

        # ----------------- Aperçu du résultat (caché tant qu'il n'y en a pas) --
        self.preview_lbl = QLabel()
        self.preview_lbl.setObjectName("preview")
        self.preview_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_lbl.setVisible(False)
        layout.addWidget(self.preview_lbl)

        # ----------------- Journal ---------------------------------------------
        log_lbl = QLabel("Journal")
        log_lbl.setObjectName("sectionHint")
        layout.addWidget(log_lbl)
        self.log_view = QTextEdit()
        self.log_view.setObjectName("logView")
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(120)
        self.log_view.setMaximumHeight(180)
        layout.addWidget(self.log_view)

        layout.addStretch(0)
        scroll.setWidget(content)
        root.addWidget(scroll, 1)

    # ---- Thème (feuille de style Qt) ----------------------------------------
    def _stylesheet(self):
        """Thème sombre « astro », appliqué à toute la fenêtre.

        Une erreur de règle QSS est ignorée par Qt (jamais fatale) ; les couleurs
        des voyants (vert/orange/rouge) sont posées en direct sur les étiquettes
        concernées et priment donc sur ce thème.
        """
        return """
        QWidget#window, QScrollArea#scroll, QWidget#content { background-color: #0f1117; }
        QWidget { color: #dfe3ee; font-size: 10.5pt; }

        /* En-tête */
        QWidget#header { background-color: #14161f; border-bottom: 1px solid #242838; }
        QLabel#title { font-size: 19pt; font-weight: 800; color: #f4f6fc; }
        QLabel#subtitle { color: #8a92a8; font-size: 9.5pt; }
        QLabel#conn { font-weight: 600; color: #8a92a8; }
        QLabel#help { color: #9aa2b8; font-size: 10pt; }
        QLabel#sectionHint { color: #8a92a8; }

        /* Cartes (sections numérotées) */
        QGroupBox {
            background-color: #171a24;
            border: 1px solid #262a38;
            border-radius: 12px;
            margin-top: 16px;
            padding: 14px 14px 12px 14px;
            font-weight: 600;
        }
        QGroupBox::title {
            subcontrol-origin: margin;
            subcontrol-position: top left;
            left: 14px;
            padding: 2px 8px;
            color: #aab2ca;
        }

        /* Champs texte */
        QLineEdit {
            background-color: #0c0f16;
            border: 1px solid #2a3145;
            border-radius: 8px;
            padding: 8px 10px;
            color: #e6e9f2;
            selection-background-color: #5b6cff;
        }
        QLineEdit:focus { border-color: #5b6cff; }
        QLineEdit:disabled { color: #6a7185; background-color: #12151d; }

        /* Menu déroulant */
        QComboBox {
            background-color: #0c0f16;
            border: 1px solid #2a3145;
            border-radius: 8px;
            padding: 8px 10px;
            color: #e6e9f2;
        }
        QComboBox:hover { border-color: #3e475f; }
        QComboBox:focus { border-color: #5b6cff; }
        QComboBox::drop-down { border: none; width: 22px; }
        QComboBox QAbstractItemView {
            background-color: #171a24;
            border: 1px solid #2a3145;
            selection-background-color: #5b6cff;
            color: #e6e9f2;
            outline: none;
            padding: 4px;
        }

        /* Cases à cocher */
        QCheckBox { spacing: 8px; padding: 3px 0; color: #dfe3ee; }
        QCheckBox:disabled { color: #6a7185; }

        /* Boutons — style par défaut (secondaire) */
        QPushButton {
            background-color: #232838;
            color: #e6e9f2;
            border: 1px solid #323a4f;
            border-radius: 8px;
            padding: 8px 14px;
        }
        QPushButton:hover { background-color: #2b3145; border-color: #3e475f; }
        QPushButton:pressed { background-color: #1d2231; }
        QPushButton:disabled { color: #6a7185; background-color: #191d28; border-color: #252a38; }

        /* Bouton principal (Lancer) */
        QPushButton#primary {
            background-color: #5b6cff;
            border: none;
            color: #ffffff;
            font-weight: 700;
            font-size: 11.5pt;
            padding: 12px 18px;
        }
        QPushButton#primary:hover { background-color: #6f7dff; }
        QPushButton#primary:pressed { background-color: #4a5ae6; }
        QPushButton#primary:disabled { background-color: #2b3150; color: #8a90ad; }

        /* Bouton d'arrêt */
        QPushButton#danger {
            background-color: transparent;
            border: 1px solid #7a2f2f;
            color: #ff8a7a;
        }
        QPushButton#danger:hover { background-color: #3a1f22; }
        QPushButton#danger:disabled { color: #5a5560; border-color: #2a2230; }

        /* Bouton discret (Aide, Parcourir) */
        QPushButton#ghost {
            background-color: transparent;
            border: 1px solid #323a4f;
            color: #cfd5e6;
            padding: 6px 14px;
        }
        QPushButton#ghost:hover { background-color: #20263a; border-color: #3e475f; }

        /* Barre de progression */
        QProgressBar {
            background-color: #0c0f16;
            border: 1px solid #2a3145;
            border-radius: 8px;
            height: 18px;
            text-align: center;
            color: #dfe3ee;
        }
        QProgressBar::chunk { background-color: #5b6cff; border-radius: 7px; }

        /* Aperçu + journal */
        QLabel#preview {
            border: 1px solid #232a3b;
            border-radius: 10px;
            background-color: #0b0e14;
            padding: 8px;
        }
        QTextEdit#logView {
            background-color: #0b0e14;
            border: 1px solid #232a3b;
            border-radius: 8px;
            color: #b9c0d4;
            font-family: Consolas, Menlo, monospace;
            font-size: 9pt;
            padding: 6px;
        }

        /* Boîtes de dialogue (À propos, messages) */
        QDialog, QMessageBox { background-color: #12141c; }
        QTextBrowser {
            background-color: #0b0e14;
            border: 1px solid #232a3b;
            border-radius: 8px;
            padding: 6px;
        }

        /* Barres de défilement */
        QScrollBar:vertical { background: transparent; width: 12px; margin: 2px; }
        QScrollBar::handle:vertical { background: #2c3346; border-radius: 6px; min-height: 30px; }
        QScrollBar::handle:vertical:hover { background: #3a445e; }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
        """

    # ---- Pipeline courant ----------------------------------------------------
    def _current_pipeline(self):
        return PIPELINES[self.pipeline_combo.currentIndex()]

    # ---- (Re)génération des cases à cocher selon le pipeline ----------------
    def _rebuild_steps(self):
        # Met à jour le petit descriptif sous le menu déroulant.
        self.pipe_desc.setText(self._current_pipeline().description)
        # Vide les cases existantes.
        while self.steps_layout.count():
            item = self.steps_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        # Crée une case par étape du pipeline courant.
        self.checkboxes = []
        for step in self._current_pipeline().post_steps:
            cb = QCheckBox(step.label)
            cb.setChecked(step.enabled)
            cb._step_key = step.key   # pour mémoriser/restaurer l'état par étape
            self.checkboxes.append(cb)
            self.steps_layout.addWidget(cb)

    # ---- Réglages mémorisés (charger / appliquer / collecter / sauver) ------
    def _load_settings(self):
        """Lit le fichier de réglages ; dict vide si absent/illisible."""
        try:
            return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _apply_settings(self, data):
        """Restaure les réglages dans l'interface (tolérant aux valeurs absentes)."""
        try:
            idx = int(data.get("pipeline_index", 0))
            if 0 <= idx < self.pipeline_combo.count():
                self.pipeline_combo.setCurrentIndex(idx)   # peut relancer _rebuild_steps
            # États des cases, par clé d'étape.
            steps = data.get("steps", {})
            for cb in self.checkboxes:
                if cb._step_key in steps:
                    cb.setChecked(bool(steps[cb._step_key]))
            self.name_edit.setText(data.get("output_name", "") or "")
            self.batch_check.setChecked(bool(data.get("batch", False)))
            self.dir_edit.setText(data.get("directory", "") or "")
        except Exception:
            pass

    def _gather_settings(self):
        """Photographie de l'état courant de l'interface."""
        return {
            "directory": self.dir_edit.text(),
            "pipeline_index": self.pipeline_combo.currentIndex(),
            "batch": self.batch_check.isChecked(),
            "output_name": self.name_edit.text(),
            "steps": {cb._step_key: cb.isChecked() for cb in self.checkboxes},
        }

    def _save_settings(self):
        """Écrit les réglages courants (échec silencieux : rien de vital)."""
        try:
            SETTINGS_PATH.write_text(
                json.dumps(self._gather_settings(), indent=2), encoding="utf-8")
        except Exception:
            pass

    # ---- Sélection du dossier -----------------------------------------------
    def _choose_dir(self):
        title = ("Choisir le dossier parent" if self.batch_check.isChecked()
                 else "Choisir le dossier de la cible")
        chosen = QFileDialog.getExistingDirectory(self, title)
        if chosen:
            self.dir_edit.setText(chosen)  # déclenche _update_folder_status

    # ---- Recherche des cibles valides (mode lot) ----------------------------
    def _find_targets(self, parent: Path):
        """Sous-dossiers de `parent` qui sont des cibles valides.

        Une cible valide contient les 4 sous-dossiers requis, chacun non vide.
        """
        targets = []
        try:
            children = sorted(p for p in parent.iterdir() if p.is_dir())
        except OSError:
            return targets
        for child in children:
            if all((child / d).is_dir() and self._count_images(child / d) > 0
                   for d in REQUIRED_DIRS):
                targets.append(child)
        return targets

    # ---- Bascule mode normal / mode lot -------------------------------------
    def _on_batch_toggled(self, checked):
        self.single_status_widget.setVisible(not checked)
        self.batch_status_widget.setVisible(checked)
        if checked:
            self.dir_hint.setText("Mode lot : choisis le dossier PARENT qui contient "
                                  "un sous-dossier par cible (chacun avec ses "
                                  "lights/darks/flats/biases).")
            self.dir_edit.setPlaceholderText("Dossier parent (un sous-dossier par cible)")
            self.name_edit.setEnabled(False)   # en lot : nom = celui du sous-dossier
            self.name_edit.setPlaceholderText("(mode lot : chaque cible garde le nom de son dossier)")
        else:
            self.dir_hint.setText("Le dossier doit contenir les sous-dossiers "
                                  "lights, darks, flats et biases.")
            self.dir_edit.setPlaceholderText("Aucun dossier choisi…")
            self.name_edit.setEnabled(True)
            self.name_edit.setPlaceholderText("result  (ex. M31, NGC7000 — vide = result)")
        self._update_folder_status()

    # ---- Comptage des images d'un dossier -----------------------------------
    def _count_images(self, folder: Path) -> int:
        """Nombre de fichiers image (RAW/FITS) directement dans le dossier.

        Non récursif : c'est exactement ce que la commande 'convert' traitera.
        """
        try:
            return sum(
                1 for f in folder.iterdir()
                if f.is_file() and f.suffix.lower() in IMAGE_EXTS
            )
        except OSError:
            return 0

    # ---- Vérification en direct du dossier ----------------------------------
    def _update_folder_status(self):
        text = self.dir_edit.text().strip()
        work = Path(text) if text else None

        # -- Mode lot : compter les cibles valides dans le dossier parent --
        if self.batch_check.isChecked():
            targets = self._find_targets(work) if (work and work.is_dir()) else []
            if targets:
                names = ", ".join(t.name for t in targets)
                self.batch_status_lbl.setText(f"✓ {len(targets)} cible(s) : {names}")
                self.batch_status_lbl.setStyleSheet("color:#3ba55d;")
                ok = True
            else:
                self.batch_status_lbl.setText(
                    "✗ Aucune cible valide (sous-dossier avec lights/darks/flats/biases non vides).")
                self.batch_status_lbl.setStyleSheet("color:#d83c3c;")
                ok = False
            self.run_btn.setEnabled(ok and self.connected and self._worker is None)
            return

        # -- Mode normal : les 4 voyants avec leur nombre d'images --
        all_ok = work is not None and work.is_dir()
        for name, lbl in self.status_labels.items():
            sub = (work / name) if work is not None else None
            if sub is not None and sub.is_dir():
                n = self._count_images(sub)
                if n > 0:
                    lbl.setText(f"✓ {name} ({n})")
                    lbl.setStyleSheet("color:#3ba55d;")
                else:
                    lbl.setText(f"⚠ {name} (0)")
                    lbl.setStyleSheet("color:#d9822b;")
                    all_ok = False
            else:
                lbl.setText(f"✗ {name}")
                lbl.setStyleSheet("color:#d83c3c;")
                all_ok = False
        # Le bouton n'est actif que si tout est bon, Siril connecté et pas de run en cours.
        self.run_btn.setEnabled(all_ok and self.connected and self._worker is None)

    # ---- Connexion à Siril ---------------------------------------------------
    def _connect_to_siril(self):
        try:
            self.siril = s.SirilInterface()
            self.siril.connect()
            self.connected = True
            self.conn_lbl.setText("●  Siril connecté")
            self.conn_lbl.setStyleSheet("color:#3ba55d;")
            self._log("Connecté à Siril.")
        except SirilConnectionError as e:
            self.connected = False
            self.conn_lbl.setText("●  Siril non connecté")
            self.conn_lbl.setStyleSheet("color:#e0503a;")
            self.run_btn.setEnabled(False)
            QMessageBox.critical(
                self, "Connexion impossible",
                f"Impossible de se connecter à Siril :\n{e}\n\n"
                "Lance ce script depuis le menu Scripts > Scripts Python de Siril."
            )

    # ---- Journalisation ------------------------------------------------------
    def _log(self, msg):
        self.log_view.append(msg)
        try:
            if self.connected:
                self.siril.log(msg)
        except Exception:
            pass
        QApplication.processEvents()

    # ---- Verrouillage des réglages pendant un traitement --------------------
    def _set_controls_enabled(self, enabled):
        """Active/désactive les réglages (le bouton Arrêter est géré à part)."""
        self.pipeline_combo.setEnabled(enabled)
        self.dir_edit.setEnabled(enabled)
        self.browse_btn.setEnabled(enabled)
        self.batch_check.setEnabled(enabled)
        # Le champ nom n'est éditable qu'en mode normal.
        self.name_edit.setEnabled(enabled and not self.batch_check.isChecked())
        for cb in self.checkboxes:
            cb.setEnabled(enabled)

    # ---- Lancement (dans un thread de fond) ---------------------------------
    def on_run(self):
        # Rien à faire si non connecté ou si un traitement tourne déjà.
        if not self.connected or self._worker is not None:
            return
        text = self.dir_edit.text().strip()
        if not text:
            QMessageBox.warning(self, "Dossier manquant", "Choisis d'abord un dossier.")
            return
        work = Path(text)
        if not work.is_dir():
            QMessageBox.warning(self, "Dossier introuvable", f"Introuvable :\n{work}")
            return

        # Construction de la liste des cibles à traiter (jobs).
        if self.batch_check.isChecked():
            targets = self._find_targets(work)
            if not targets:
                QMessageBox.warning(self, "Aucune cible",
                                    "Aucun sous-dossier valide trouvé dans ce dossier parent.")
                return
            # En lot, chaque cible garde le nom de son dossier.
            jobs = [(t, sanitize_name(t.name)) for t in targets]
            open_path = work
        else:
            missing = [d for d in REQUIRED_DIRS if not (work / d).is_dir()]
            if missing:
                QMessageBox.warning(self, "Sous-dossiers manquants",
                                    "Introuvables : " + ", ".join(missing))
                return
            jobs = [(work, sanitize_name(self.name_edit.text()))]
            open_path = work

        pipeline = self._current_pipeline()
        # État des cases capturé maintenant (le worker ne touche pas à la GUI).
        enabled_flags = [cb.isChecked() for cb in self.checkboxes]

        # Verrouillage de l'interface + remise à zéro de la progression.
        self._set_controls_enabled(False)
        self.run_btn.setEnabled(False)
        self.run_btn.setText("Traitement en cours…")
        self.stop_btn.setEnabled(True)
        self.log_view.clear()
        self.progress.setValue(0)
        self.preview_lbl.clear()
        self.preview_lbl.setVisible(False)
        self._save_settings()   # mémorise ce lancement (dossier, type, cases…)

        # Démarrage du thread : SEUL le worker parlera à Siril.
        self._worker = ProcessingWorker(self.siril, jobs, pipeline, enabled_flags)
        self._worker.sig_log.connect(self._append_log)
        self._worker.sig_progress.connect(self._on_progress)
        self._worker.sig_phase.connect(self.phase_lbl.setText)
        self._worker.sig_done.connect(
            lambda ok, msg: self._on_done(ok, msg, jobs, open_path))
        self._worker.start()

    # ---- Demande d'arrêt -----------------------------------------------------
    def on_stop(self):
        if self._worker is not None:
            self._worker.request_stop()   # arrêt coopératif à la fin de l'étape
            self.stop_btn.setEnabled(False)
            self.stop_btn.setText("Arrêt…")
            self.phase_lbl.setText("Arrêt demandé — fin de l'étape en cours…")

    # ---- Réactions aux signaux du worker (sur le thread principal) ----------
    def _append_log(self, msg):
        """Ajoute une ligne au journal. GUI uniquement — aucun appel à Siril ici."""
        self.log_view.append(msg)

    def _on_progress(self, done, total):
        self.progress.setMaximum(total)
        self.progress.setValue(done)

    def _on_done(self, success, message, jobs, open_path):
        # Attendre la fin effective du thread avant de lâcher sa référence.
        if self._worker is not None:
            self._worker.wait()
        self._worker = None
        # Fin (succès, erreur ou arrêt) : on déverrouille l'interface.
        self._set_controls_enabled(True)
        self.run_btn.setText("Lancer le traitement")
        self.stop_btn.setText("Arrêter")
        self.stop_btn.setEnabled(False)
        self._update_folder_status()   # réactive « Lancer » si tout est encore bon

        if success:
            self.phase_lbl.setText("Terminé.")
            names = ", ".join(n for _, n in jobs)
            self._append_log(f"=== Terminé ({len(jobs)} cible(s)) : {names} ===")
            # Aperçu de la dernière cible traitée (result du mode normal).
            last_work, last_name = jobs[-1]
            self._show_preview(last_work / f"{last_name}_preview.png")
            self._show_done(jobs, open_path)
        elif "arrêté" in message.lower():
            # Arrêt volontaire : ce n'est pas une erreur.
            self.phase_lbl.setText("Arrêté.")
            self._append_log("--- " + message + " ---")
            QMessageBox.information(self, "Arrêté", message)
        elif message.startswith("Connexion perdue"):
            self.connected = False
            self.conn_lbl.setText("●  Siril non connecté")
            self.conn_lbl.setStyleSheet("color:#e0503a;")
            self.phase_lbl.setText("Connexion perdue.")
            self._append_log("ERREUR : " + message)
            QMessageBox.critical(self, "Connexion perdue", message)
        else:
            self.phase_lbl.setText("Erreur.")
            self._append_log("ERREUR : " + message)
            QMessageBox.critical(self, "Erreur de traitement",
                                 "Le traitement s'est arrêté :\n" + message)

    # ---- Aperçu (vignette du PNG exporté) -----------------------------------
    def _show_preview(self, png_path: Path):
        """Charge le PNG d'aperçu dans la vignette (silencieux si absent/illisible)."""
        try:
            if not png_path.is_file():
                return
            pix = QPixmap(str(png_path))
            if pix.isNull():
                return
            # Réduction pour la vignette, en gardant les proportions.
            scaled = pix.scaled(
                560, 340,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self.preview_lbl.setPixmap(scaled)
            self.preview_lbl.setVisible(True)
        except Exception:
            pass

    # ---- Fermeture de la fenêtre --------------------------------------------
    def closeEvent(self, event):
        """Ne pas tuer brutalement un traitement en cours à la fermeture."""
        if self._worker is not None and self._worker.isRunning():
            reply = QMessageBox.question(
                self, "Traitement en cours",
                "Un traitement est en cours. L'arrêter et fermer la fenêtre ?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self._worker.request_stop()
            self._worker.wait(5000)   # laisse jusqu'à 5 s la commande en cours finir
        self._save_settings()   # mémorise les réglages pour la prochaine fois
        event.accept()

    # ---- Fenêtre de fin (avec ouverture du dossier) -------------------------
    def _show_done(self, jobs, open_path: Path):
        box = QMessageBox(self)
        box.setWindowTitle("Terminé")
        if len(jobs) == 1:
            work, name = jobs[0]
            box.setText("Traitement terminé.")
            box.setInformativeText(
                f"Fichiers créés dans :\n{work}\n\n"
                f"• {name}.fit  (linéaire, brut)\n"
                f"• {name}_processed.tif  (TIFF 16-bit traité)"
            )
        else:
            names = ", ".join(n for _, n in jobs)
            box.setText(f"{len(jobs)} cibles traitées.")
            box.setInformativeText(
                f"Cibles : {names}\n\n"
                "Chaque cible contient son <nom>.fit (linéaire) et "
                "<nom>_processed.tif (traité)."
            )
        open_btn = box.addButton("Ouvrir le dossier", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Fermer", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is open_btn:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(open_path)))

    # ---- Fenêtre « À propos / Aide » ----------------------------------------
    def show_about(self):
        """Aide générée à partir des données (reste à jour si le pipeline évolue)."""
        # Étapes optionnelles du pipeline actuellement sélectionné.
        optional = "".join(
            f"<li>{step.label}</li>" for step in self._current_pipeline().post_steps
        )
        # Liste de tous les pipelines disponibles.
        pipelines_html = "".join(
            f"<li><b>{p.label}</b><br>{p.description}</li>" for p in PIPELINES
        )

        html = f"""
        <style>
          body {{ color:#dfe3ee; font-family:'Segoe UI',Arial,sans-serif; line-height:1.5; }}
          h2 {{ color:#f4f6fc; }}
          h3 {{ color:#9aa8ff; margin-top:18px; }}
          code {{ background:#232838; color:#e6e9f2; padding:1px 5px; border-radius:4px; }}
          a {{ color:#7c8cff; }}
          li {{ margin-bottom:5px; }}
        </style>
        <h2>OSC Studio — que fait cette app ?</h2>
        <p>Elle traite une cible de bout en bout dans Siril, à partir de tes
        photos rangées dans <code>lights / darks / flats / biases</code>.</p>

        <h3>Types de cible disponibles</h3>
        <ul>{pipelines_html}</ul>

        <h3>Étapes toujours exécutées (non désactivables)</h3>
        <ol>
          <li>Calibration (darks + flats + biases)</li>
          <li>Alignement des images (registration)</li>
          <li>Empilement (stacking)</li>
          <li>Sauvegarde de <code>result.fit</code> — image linéaire brute</li>
          <li>Sauvegarde de <code>result_processed.tif</code> — TIFF 16-bit final</li>
        </ol>

        <h3>Étapes optionnelles du type sélectionné (les cases à cocher)</h3>
        <ul>{optional}</ul>
        <p>Une case décochée = l'étape est simplement sautée.</p>

        <h3>À régler une seule fois (pour des couleurs justes)</h3>
        <p>Ouvre l'outil <b>SPCC</b> en mode graphique, choisis ton capteur
        (Canon R8), ton filtre et la référence de blanc, puis lance-le une fois.
        Le pipeline couleur réutilisera ensuite ces réglages.</p>

        <p style="color:gray;">Astuce : lance une fois tout coché, une fois tout
        décoché, et compare les deux <code>result_processed.tif</code>.</p>
        """

        dlg = QDialog(self)
        dlg.setWindowTitle("À propos / Aide")
        dlg.setMinimumSize(580, 520)
        lay = QVBoxLayout(dlg)
        browser = QTextBrowser()
        browser.setHtml(html)
        browser.setOpenExternalLinks(True)
        lay.addWidget(browser)
        close_btn = QPushButton("Fermer")
        close_btn.clicked.connect(dlg.accept)
        lay.addWidget(close_btn)
        dlg.exec()


# =============================================================================
#  POINT D'ENTRÉE
# =============================================================================

def main():
    app = QApplication(sys.argv)
    # Fusion : rendu identique sur macOS et Windows, respecte bien la feuille de style.
    try:
        app.setStyle("Fusion")
    except Exception:
        pass
    win = OscStudioWindow()
    win.show()
    app.exec()


if __name__ == "__main__":
    main()
