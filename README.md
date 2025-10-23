# Sonarr

## calculate-tv-shows.py 
Ce script permet de lister quelle séries à le plus pros stockage en fonctione du nombre total d'épisode.
Il nous rendra un liste dans le terminal.

Lancer et afficher tout trié :
```
python calculate-tv-shows.py --url http://192.168.1.10:8989 --api-key APIKEY-ABCDEFGHIJK
```
Export CSV + top 20 :
```
python calculate-tv-shows.py --url http://sonarr:8989 --api-key ABC --out csv --out-file top20.csv --top 20
```