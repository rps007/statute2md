# statute2md

Öffne [statute2md.ipynb in Colab](https://colab.research.google.com/github/rps007/statute2md/blob/main/statute2md.ipynb) und führe die GUI-Zelle aus. In einer frischen Colab-Sitzung klont die Startzelle das Repository nach `/content/statute2md`. Hinterlege dafür die Colab-Secrets `gittoken` und `github_username` und aktiviere den Notebook-Zugriff. `github_username` enthält deinen GitHub-Benutzernamen, beispielsweise `rps007`. Clone, Pull, Commit und Push verwenden immer dessen Repository `<github_username>/statute2md` auf `main`. Die Arbeitsordner sind `/content/statute2md/xml-sources` und `/content/statute2md/md_files`. Verwende für Commits bei Bedarf zusätzlich `gitemail`; der Commit-Name entspricht `github_username`. Lokal startest du Jupyter aus der Arbeitskopie. Die Startzelle stellt `ipywidgets` bei Bedarf über `uv` bereit. Oben stehen der XML-Download und der lokale ZIP-Import. Unter **Dateioperationen auf Colab** nutzt du dieselbe XML-Liste für Transformation, Löschen und Download. **Anbindung an Github** steht ganz unten.

| Aktion | Funktion |
| --- | --- |
| **Add Sources** | Gesetzeskürzel eingeben. Die XML-Datei wird in `xml-sources/` gespeichert. Bei Colab-Netzwerkfehlern lade das Original-ZIP über die Dateileiste nach `/content/statute2md/xml-sources` hoch und trage den Dateinamen ein. Ein vollständiger Pfad funktioniert ebenfalls. |
| **Transform** | Eine oder mehrere XML-Dateien auswählen und nach `md_files/` konvertieren. Der Fortschrittsbalken zeigt die Verarbeitung bis zum geprüften und gespeicherten Export. Nur die ausgewählten Gesetze werden gerendert; andere Markdown-Dateien bleiben unverändert. Alle erzeugten Verweise sind ungeprüfte Vorratslinks. |
| **Delete** | Ausgewählte XML- und Markdown-Dateien löschen. Verweise in den verbleibenden Dateien bleiben als Vorratslinks erhalten. |
| **Download** | Genau eine ausgewählte Markdown-Datei wird direkt heruntergeladen, auch wenn XML-Dateien markiert sind. Bei anderen Auswahlen wird eine einzelne Datei direkt und eine Mehrfachauswahl als ZIP heruntergeladen. |
| **Obsidian-ZIP** | Den gesamten Export mit ungeprüften Vorratslinks herunterladen. Den enthaltenen Ordner `Bundesrecht/` direkt in die Vault-Wurzel entpacken. |
| **Clone** | Die bestehende Arbeitskopie unter `/content/statute2md` auf `main` per Fast-Forward aktualisieren. |
| **Commit / Push** | Mit einer Nachricht Änderungen in den beiden Datenordnern einchecken und Commits auf `main` übertragen. |

Die Original-ZIPs und ihre Provenienz liegen in `xml-sources/_original/`. Der Link-Index erklärt unter anderem offene Querverweise. In `md_files/` liegen auch das Obsidian-ZIP und das Quellprojekt mit den XML-Versionen des exportierten Bestands.

Alle neu erzeugten Links erhalten im Link-Index den Status `unchecked`; weder die Existenz der Zieldatei noch die des Zielblocks wird geprüft. Beispielsweise entsteht `[[Bundesrecht/bgb#^p-5|§ 5 BGB]]` auch ohne hochgeladenes BGB. Ein späterer BGB-Export aktiviert den Link in Obsidian ohne erneuten HGB-Export. Dateinamen werden aus bekannten Gesetzeskürzeln und dem kanonischen Slug abgeleitet. Bei unbekanntem Slug, mehrdeutigem Bezug oder historischen Fassungen bleibt der Verweis als Text mit einem offenen Indexbefund erhalten.

Bei Nummern und Buchstaben kann die genaue Block-ID von der XML-Struktur abhängen. Solche Vorratslinks führen zum syntaktisch ableitbaren übergeordneten Absatz, Satz oder zur Norm. Der Index enthält dafür `target_precision: parent` und den tatsächlichen `target_selector`; es werden keine Listen-IDs erraten. Die Ausgabeprüfung kontrolliert weiterhin Quelltext, Format, eigene Blockpositionen sowie die Übereinstimmung von Markdown-Links und Index. Sie bestätigt ausdrücklich keine Linkziele.

Der Gesamtbestand, der Link-Index und die beiden ZIP-Dateien werden beim Einzelrendern aktualisiert, ohne andere Gesetze neu zu rendern. Vorhandene ältere Links werden im Gesamtindex als ungeprüft übernommen; um ihre bisherigen Ziel-IDs auf das neue Verfahren umzustellen, müssen die jeweiligen Gesetze einmal ausgewählt und transformiert werden.

Das Protokoll sammelt Einträge nur während der laufenden GUI-Sitzung. Beim erneuten Ausführen der GUI-Zelle wird das alte Protokoll geleert; die neue Oberfläche startet ohne frühere Einträge.

Für Obsidian steht die Block-ID eines Paragrafen oder Artikels am Ende seiner Überschrift, etwa `§ 2 — Persönliche Steuerpflicht ^p-2`. Nummerierte Absätze beginnen mit einer fetten, farblich hervorgehobenen Zeile wie **Absatz 1**. Auch Gliederungszeichen im Normtext wie `1.` und `a)` sind hervorgehoben. Satz- und Listenblock-IDs bleiben am zugehörigen Text erhalten. Bei tiefen Gliederungen erscheint der vollständige Pfad einmal je gemeinsamem Elternabschnitt. Erzeuge vorhandene Markdown-Dateien mit **Transform** erneut, um die Darstellung zu übernehmen.

Die Startzelle liest die Colab-Secrets für die laufende Sitzung ein. Gib dem Notebook bei fehlendem `gittoken` oder `github_username` über das Schlüssel-Symbol Zugriff und führe die Startzelle erneut aus. Im Git-Protokoll zeigt `clone` einen neuen Klon und `pull` die Aktualisierung einer vorhandenen Arbeitskopie an. Ein Git-Aufruf hat ein Zeitlimit von 45 Sekunden.

**Add Sources** protokolliert die konkrete URL, den HTTPS-Verbindungsaufbau, den HTTP-Status und die empfangene Größe vor der ZIP-Prüfung. Der Abruf berücksichtigt einen konfigurierten HTTP-CONNECT-Proxy für HTTPS und `NO_PROXY`. Ein erfolgreicher Download im Browser belegt die Erreichbarkeit vom eigenen Rechner. Für den direkten Abruf muss die Colab-Laufzeit den Host selbst erreichen. Bei einem Verbindungsfehler zeigt die GUI den Original-ZIP-Link für den Browser an.

## Entwicklung und Prüfung

Die Konvertierung steckt in `statute_source.py`, `statute_model.py`, `statute_links.py` und `statute_export.py`. `repository_files.py`, `repository_git.py` und `repository_gui.py` verbinden diese Module mit der Arbeitskopie und dem Notebook.

```bash
python -m unittest discover -s tests -v
```

Die Tests verwenden archivierte XML-Ausschnitte. Sie prüfen unter anderem den Mehrfachexport, das Entfernen eines Gesetzes bei unveränderten Vorratslinks, den Download und den Git-Commit im lokalen Test-Repository. Der Live-Abruf aus Colab und die Bedienung im Colab-Browser wurden für diese Version nicht manuell geprüft.

Nach einem vollständig ersetzten Git-Verlauf erneuert die Colab-Startzelle eine veraltete Arbeitskopie automatisch. Sie übernimmt `xml-sources/` und `md_files/` und behält die gesamte alte Arbeitskopie einschließlich lokaler Commits und weiterer Dateien unter `/content/statute2md-recovery-…/previous`. Der genaue Sicherungspfad steht im Startprotokoll. Bei einem fehlgeschlagenen Abruf bleibt die ursprüngliche Arbeitskopie erhalten. Lade nach einer Notebook-Aktualisierung auch die Startzelle im Browser neu; eine bereits geöffnete alte Zelle aktualisiert ihren eigenen Code nicht.
