# Fallout Multiverse Item Scraper & Sync Tool

## Overview
This enhanced scraper does three things:
1. **Scans** Unturned/Fallout Multiverse bundles and workshop folders
2. **Exports** items to XLSX with proper categorization
3. **Syncs** to Google Sheets (smart deduplication, no overwrites unless changed)
4. **Uploads** .dat and .asset files to GitHub

## Prerequisites
- Python 3.10+
- `.env.txt` with Google Sheets ID and GitHub token
- `credentials.json` for Google Sheets authentication

## Configuration (.env.txt)
```
SPREADSHEET_ID=Spreadsheet ID
GITHUB_TOKEN=your_github_token_here
GITHUB_REPO= 
```

## Usage

### Full Sync (Everything)
```bash
python Test.py
```
- Exports to XLSX
- Syncs with Google Sheets
- Uploads files to GitHub

### Skip Google Sheets
```bash
python Test.py --skip-google-sheets
```

### Skip GitHub
```bash
python Test.py --skip-github
```

### Preview Only (No Export)
```bash
python Test.py --scan-only
```

### Custom Folders
```bash
python Test.py --bundles-folder "D:\CustomPath\Bundles" --workshop-folder "D:\CustomPath\Workshop"
```

## Google Sheets Behavior
- **Auto-creates** sheets by category if they don't exist
- **Prevents duplicates** - checks if ID already exists
- **Smart updates** - only updates if values changed (ID, GUID, Name, Description)
- **No data loss** - existing data preserved unless explicitly changed

## GitHub Upload
- Uploads to: `tonyseedgaming-source/FalloutMultiverseIDs/Data`
- Folder structure: `Category/ItemFolder/Filename`
- Creates or updates files
- Uses base64 encoding for binary data

## Category Mapping
The script uses official Fallout Multiverse categories:
- Supplies
- Workshops
- Blueprints
- Utilities
- Entertainment
- Structures
- Buildables
- Clothing
- Consumables
- Melee Weapons
- Guns
- Attachments
- Ammunition
- Throwables
- Animals
- Spawns
- Trees
- Vehicles
- Effects
- Items
- Objects
- Plants
- Resources

## Troubleshooting

### Google Sheets Auth Error
Ensure `credentials.json` is in the same directory as the script and has proper permissions.

### GitHub Upload Fails
- Check `GITHUB_TOKEN` in `.env.txt` has `repo` scope
- Verify `GITHUB_REPO` path is correct
- Ensure token hasn't expired

### Missing Dependencies
The script auto-installs missing packages (openpyxl, gspread, google-auth, requests)

## Files Generated
- `UnturnedItems.xlsx` - Main export file with all items
- Google Sheets - Synced with categories
- GitHub `/Data` folder - Contains all .dat and .asset files

## Performance
- Scans ~4,000+ folders
- Extracts item metadata from .dat files
- Syncs 500+ items in under 1 minute
- GitHub uploads depend on file count and size

## Support
For issues, check:
1. `.env.txt` configuration
2. `credentials.json` permissions
3. GitHub token scope (needs `repo`)
4. Internet connection for Google Sheets/GitHub APIs
