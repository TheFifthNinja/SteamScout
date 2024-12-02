import requests

def fetch_game_details(app_id, api_key):
    url = f"http://store.steampowered.com/api/appdetails"
    
    params = {
        "appids": app_id,
        "key": api_key,
    }
    
    response = requests.get(url, params=params)
    
    if response.status_code != 200:
        print("Error: Unable to fetch game details.")
        return None
    
    data = response.json()
    
    if str(app_id) in data and data[str(app_id)]["success"]:
        game_data = data[str(app_id)]["data"]
        name = game_data.get("name", "Unknown Game")
        requirements = game_data.get("pc_requirements", {})
        
        return {
            "name": name,
            "minimum_requirements": requirements.get("minimum", "Not provided"),
            "recommended_requirements": requirements.get("recommended", "Not provided"),
        }
    else:
        print(f"Error: Could not find details for app ID {app_id}")
        return None

if __name__ == "__main__":
    API_KEY = "86BC15CF4164D2AAE629A93945A1452B"
    
    APP_ID = 2072450
    
    game_details = fetch_game_details(APP_ID, API_KEY)
    if game_details:
        print(f"Game: {game_details['name']}")
        print("Minimum Requirements:")
        print(game_details['minimum_requirements'])
        print("Recommended Requirements:")
        print(game_details['recommended_requirements'])
