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
import sys

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
    QMessageBox, QDialog, QTextBrowser,
)
from PyQt6.QtCore import QUrl
from PyQt6.QtGui import QDesktopServices


# =============================================================================
#  DÉFINITION DES PIPELINES (données, pas de logique)
# =============================================================================

# Sous-dossiers attendus dans le dossier de la cible.
REQUIRED_DIRS = ("lights", "darks", "flats", "biases")

# Sauvegarde finale commune : TIFF 16-bit (savetif écrit du 16-bit par défaut).
SAVE_TIFF = [("savetif", "../result_processed")]


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
    # Sauvegarde de l'image LINÉAIRE brute (result.fit dans le dossier cible).
    ("load", "result"),
    ("mirrorx", "-bottomup"),
    ("save", "../result"),
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
    # Composition HOO : R=Ha, G=OIII, B=OIII -> result.fit linéaire.
    ("rgbcomp", "r_results_00001 OIII_renorm OIII_renorm -out=result"),
    ("load", "result"),
    ("save", "../result"),
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
    fixed: list           # étapes fixes : calibration ... -> result.fit
    post_steps: list      # étapes optionnelles (cases à cocher)
    save_tiff: list = field(default_factory=lambda: list(SAVE_TIFF))


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
#  INTERFACE GRAPHIQUE
# =============================================================================

class OscStudioWindow(QWidget):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("OSC Studio — Siril")
        self.setMinimumWidth(660)

        self.siril = None
        self.connected = False
        self.checkboxes = []          # cases à cocher du pipeline courant
        self.status_labels = {}       # étiquettes ✓/✗ par sous-dossier

        self._build_ui()
        self._connect_to_siril()
        self._rebuild_steps()          # remplit les cases du pipeline par défaut
        self._update_folder_status()   # état initial (bouton désactivé)

    # ---- Construction de l'interface ----------------------------------------
    def _build_ui(self):
        layout = QVBoxLayout(self)

        # -- En-tête : titre + bouton d'aide --
        header = QHBoxLayout()
        header.addWidget(QLabel("<b>OSC Studio</b> — traitement caméra couleur"))
        header.addStretch(1)
        about_btn = QPushButton("ℹ  À propos / Aide")
        about_btn.clicked.connect(self.show_about)
        header.addWidget(about_btn)
        layout.addLayout(header)

        intro = QLabel(
            "1) Choisis le type de cible.  2) Choisis le dossier de la cible "
            "(il doit contenir lights, darks, flats, biases).  3) Coche les "
            "étapes et clique « Lancer »."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        # -- 1. Type de cible (pipeline) --
        pipe_row = QHBoxLayout()
        pipe_row.addWidget(QLabel("Type de cible :"))
        self.pipeline_combo = QComboBox()
        for p in PIPELINES:
            self.pipeline_combo.addItem(p.label)
        self.pipeline_combo.currentIndexChanged.connect(self._rebuild_steps)
        pipe_row.addWidget(self.pipeline_combo, 1)
        layout.addLayout(pipe_row)

        # -- 2. Dossier de la cible --
        dir_row = QHBoxLayout()
        dir_row.addWidget(QLabel("Dossier de la cible :"))
        self.dir_edit = QLineEdit()
        self.dir_edit.setPlaceholderText("Dossier contenant lights / darks / flats / biases")
        self.dir_edit.textChanged.connect(self._update_folder_status)
        dir_row.addWidget(self.dir_edit, 1)
        browse_btn = QPushButton("Parcourir…")
        browse_btn.clicked.connect(self._choose_dir)
        dir_row.addWidget(browse_btn)
        layout.addLayout(dir_row)

        # -- Voyants des 4 sous-dossiers (vérif en direct) --
        status_row = QHBoxLayout()
        status_row.addWidget(QLabel("Sous-dossiers :"))
        for name in REQUIRED_DIRS:
            lbl = QLabel(f"✗ {name}")
            self.status_labels[name] = lbl
            status_row.addWidget(lbl)
        status_row.addStretch(1)
        layout.addLayout(status_row)

        # -- 3. Étapes optionnelles (remplies selon le pipeline) --
        self.steps_box = QGroupBox("Étapes optionnelles (les fixes tournent toujours)")
        self.steps_layout = QVBoxLayout(self.steps_box)
        layout.addWidget(self.steps_box)

        # -- Bouton de lancement --
        self.run_btn = QPushButton("Lancer le traitement")
        self.run_btn.clicked.connect(self.on_run)
        layout.addWidget(self.run_btn)

        # -- Journal --
        layout.addWidget(QLabel("Journal :"))
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        layout.addWidget(self.log_view, 1)

    # ---- Pipeline courant ----------------------------------------------------
    def _current_pipeline(self):
        return PIPELINES[self.pipeline_combo.currentIndex()]

    # ---- (Re)génération des cases à cocher selon le pipeline ----------------
    def _rebuild_steps(self):
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
            self.checkboxes.append(cb)
            self.steps_layout.addWidget(cb)

    # ---- Sélection du dossier -----------------------------------------------
    def _choose_dir(self):
        chosen = QFileDialog.getExistingDirectory(self, "Choisir le dossier de la cible")
        if chosen:
            self.dir_edit.setText(chosen)  # déclenche _update_folder_status

    # ---- Vérification en direct des sous-dossiers ---------------------------
    def _update_folder_status(self):
        text = self.dir_edit.text().strip()
        work = Path(text) if text else None
        all_ok = work is not None and work.is_dir()
        for name, lbl in self.status_labels.items():
            ok = work is not None and (work / name).is_dir()
            lbl.setText(f"{'✓' if ok else '✗'} {name}")
            lbl.setStyleSheet("color:#3ba55d;" if ok else "color:#d83c3c;")
            all_ok = all_ok and ok
        # Le bouton n'est actif que si tout est bon ET Siril est connecté.
        self.run_btn.setEnabled(all_ok and self.connected)

    # ---- Connexion à Siril ---------------------------------------------------
    def _connect_to_siril(self):
        try:
            self.siril = s.SirilInterface()
            self.siril.connect()
            self.connected = True
            self._log("Connecté à Siril.")
        except SirilConnectionError as e:
            self.connected = False
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

    # ---- Exécution d'une commande Siril -------------------------------------
    def _exec(self, command, *tokens):
        """Exécute une commande Siril déjà découpée en jetons."""
        line = " ".join([command, *tokens]).strip()
        self._log(f"> {line}")
        self.siril.cmd(command, *tokens)   # signature sirilpy 1.4 : cmd(cmd, *args)

    def _exec_pair(self, command, argstring):
        """Exécute un couple (commande, chaîne d'arguments).

        La chaîne est découpée sur les espaces. Les arguments des étapes ne
        contiennent jamais d'espace (chemins simples, expression PixelMath sans
        espace). Le seul chemin pouvant contenir des espaces — le dossier de la
        cible — est traité à part dans _execute_pipeline (entre guillemets).
        """
        tokens = argstring.split() if argstring else []
        self._exec(command, *tokens)

    # ---- Lancement -----------------------------------------------------------
    def on_run(self):
        if not self.connected:
            return
        work = Path(self.dir_edit.text().strip())
        # La validation gère déjà l'activation du bouton, mais on double-vérifie.
        missing = [d for d in REQUIRED_DIRS if not (work / d).is_dir()]
        if missing:
            QMessageBox.warning(
                self, "Sous-dossiers manquants",
                "Introuvables : " + ", ".join(missing)
            )
            return

        pipeline = self._current_pipeline()

        # Verrouillage de l'interface pendant le traitement. Les commandes
        # tournent sur le thread principal (patron officiel des scripts GUI) ;
        # la fenêtre peut sembler occupée durant les étapes longues.
        self.run_btn.setEnabled(False)
        self.run_btn.setText("Traitement en cours…")
        QApplication.processEvents()

        try:
            self._execute_pipeline(work, pipeline)
            self._log("=== Terminé : result.fit + result_processed.tif ===")
            self._show_done(work)
        except SirilConnectionError as e:
            self.connected = False
            self._log(f"ERREUR de connexion : {e}")
            QMessageBox.critical(self, "Connexion perdue",
                                 f"La connexion à Siril a été perdue :\n{e}")
        except Exception as e:
            self._log(f"ERREUR : {e}")
            QMessageBox.critical(self, "Erreur de traitement",
                                 f"Le traitement s'est arrêté :\n{e}")
        finally:
            self.run_btn.setText("Lancer le traitement")
            self._update_folder_status()   # réactive le bouton si tout est encore bon

    # ---- Le pipeline lui-même -----------------------------------------------
    def _execute_pipeline(self, work: Path, pipeline: Pipeline):
        # Se placer dans le dossier de la cible (POSIX + guillemets pour les espaces).
        self._exec("cd", f'"{work.as_posix()}"')

        # Étapes fixes : calibration / alignement / empilement -> result.fit.
        self._log(f"--- [{pipeline.label}] Calibration / alignement / empilement ---")
        for command, argstring in pipeline.fixed:
            self._exec_pair(command, argstring)

        # Étapes optionnelles (cases à cocher).
        for step, cb in zip(pipeline.post_steps, self.checkboxes):
            if cb.isChecked():
                self._log(f"--- {step.label} ---")
                for command, argstring in step.commands:
                    self._exec_pair(command, argstring)
            else:
                self._log(f"--- (sauté) {step.label} ---")

        # Sauvegarde finale du TIFF 16-bit (toujours).
        self._log("--- Sauvegarde du TIFF 16-bit final ---")
        for command, argstring in pipeline.save_tiff:
            self._exec_pair(command, argstring)

        # Nettoyage.
        self._exec("cd", "..")
        self._exec("close")

    # ---- Fenêtre de fin (avec ouverture du dossier) -------------------------
    def _show_done(self, work: Path):
        box = QMessageBox(self)
        box.setWindowTitle("Terminé")
        box.setText("Traitement terminé.")
        box.setInformativeText(
            f"Fichiers créés dans :\n{work}\n\n"
            "• result.fit  (linéaire, brut)\n"
            "• result_processed.tif  (TIFF 16-bit traité)"
        )
        open_btn = box.addButton("Ouvrir le dossier", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Fermer", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is open_btn:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(work)))

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
    win = OscStudioWindow()
    win.show()
    app.exec()


if __name__ == "__main__":
    main()
