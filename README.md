En résumé, ton flux de tous les jours :

Ouvre VS Code
Ctrl+R → choisis find_job [WSL: Ubuntu]

ou bien

Ouvre un terminal Ubuntu (wsl)
cd ~/git_projects/find_job && code .

Profils disponibles :

```bash
./run.sh                  # lance lettres + comptabilité
./run.sh lettres          # lance seulement Lettres modernes
./run.sh comptabilite     # lance seulement comptabilité
./view.sh                 # lance tout et ouvre docs/index.html
./view.sh comptabilite    # lance comptabilité et ouvre son rapport local
./publish.sh              # publie les deux rapports GitHub Pages
```
