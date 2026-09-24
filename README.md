# Siril OSC Studio

**Version 0.3.0 🚧**

Traitement guidé pour **caméra couleur (OSC)** dans [Siril](https://siril.org) 1.4.x,
pensé pour s'enchaîner avec le plugin N.I.N.A. [Mode Débutant](https://github.com/TSpwn/ModeDebutant) :
on lui donne **le dossier de la nuit tel que N.I.N.A. l'a écrit**, il trouve les
cibles et va jusqu'au TIFF 16-bit fini.

*Guided processing for one-shot-color cameras in Siril 1.4.x. Point it at the
night folder written by N.I.N.A.; it finds the targets, adapts the calibration
to whatever frames exist, and goes all the way to a finished 16-bit TIFF.*

---

## 🇫🇷 Français

### Le principe

```
Astro/                         ← où N.I.N.A. enregistre (ex. C:\Astro)
├── 2026-09-23/                ← LA NUIT : c'est ce dossier qu'on choisit
│   ├── LIGHT/                 ← obligatoire
│   ├── DARK/  FLAT/  BIAS/    ← facultatifs
│   ├── M81.fit                ← sortie : empilement linéaire
│   ├── M81_processed.tif      ← sortie : image finie
│   └── _OSC_Studio/           ← travail intermédiaire (liens, jamais tes originaux)
└── _Bibliotheque_Darks/       ← masters darks, réutilisés d'une nuit à l'autre
```

1. **La nuit** — choisis le dossier daté (ou « 📅 Dernière nuit »). Les compteurs
   montrent ce qu'il contient.
2. **Les cibles** — N.I.N.A. range toutes les cibles d'une nuit dans le même
   `LIGHT/` ; l'app les sépare d'après l'en-tête FITS (cible, pose, gain). Une
   carte par série, avec ce qu'elle aura : darks, flats, calibration couleur.
3. **Le traitement** — type de cible, étapes, **Lancer**. Aperçu à la fin.

### Seules les photos sont obligatoires

| Il manque… | Ce que fait l'app |
|---|---|
| Darks | Prend un master de la **bibliothèque** aux mêmes réglages (pose, gain, offset, température). Sinon, sans darks. |
| Flats | Pas de correction du vignettage ; le retrait du gradient en rattrape une partie. |
| Bias (avec flats) | **Bias synthétique** calculé depuis l'OFFSET écrit par N.I.N.A. (caméras connues : SV405CC). |

Chaque master dark fabriqué rejoint la bibliothèque : **des darks faits une fois**
(bouchon + tissu noir, mêmes réglages, même température) servent ensuite à toutes
les nuits.

### Détails qui comptent

- **Photos pendant le refroidissement écartées** (option) : comparées à la
  consigne *finale* de la série — N.I.N.A. écrit la consigne intermédiaire de sa
  rampe dans `SET-TEMP`.
- **Résolution astrométrique** centrée sur `OBJCTRA/OBJCTDEC` (la cible centrée par
  N.I.N.A.), pas sur `RA/DEC` (la position que *croit* la monture, fausse de
  plusieurs degrés sans synchronisation).
- **SPCC** : capteur reconnu d'après `INSTRUME` (SV405CC → Sony IMX294…). Il faut
  Internet (Gaia DR3). En cas d'échec, l'image sort quand même.
- **Gradient** : `subsky -rbf` en couleur (ciel de ville) ; plan de degré 1 en
  bande étroite (préserve les grandes nébulosités).
- L'ordre est critique : SPCC sur données **linéaires**, puis `autostretch -linked`.

### Installation

1. Copie `OSC_Studio.py` dans un dossier à toi (ex. `Documents/Siril-scripts`).
2. Siril : **☰ → Préférences → Scripts**, ajoute ce dossier, **Refresh**, **Apply**.
3. **Scripts → Scripts Python → OSC_Studio**. PyQt6 s'installe tout seul au
   premier lancement.

Les deux `.ssf` (`OSC_Full_Color`, `OSC_Nebula_HaOIII`) restent disponibles pour
l'ancienne organisation `lights/darks/flats/biases` complète.

### Testé

Siril 1.4.4, sur les vraies données d'une nuit N.I.N.A. (SV405CC + 135 mm, M81) :
darks de la nuit, master de bibliothèque, flats + bias synthétique, résolution et
SPCC (1 219 étoiles). L'interface Qt, elle, n'a pas encore tourné sur macOS.

---

## 🇬🇧 English (short)

Pick the **night folder** written by N.I.N.A. (`LIGHT/`, optional `DARK/`, `FLAT/`,
`BIAS/`). Targets are split from FITS headers. Missing darks → matching master from
`_Bibliotheque_Darks` (filled automatically); missing bias with flats → synthetic
bias from `OFFSET` (known cameras). Plate solve is centred on `OBJCTRA/OBJCTDEC`,
SPCC sensor from `INSTRUME`. Outputs `<Target>.fit` and `<Target>_processed.tif`
in the night folder; your original frames are never modified.

### License

MIT — see [LICENSE](LICENSE).
