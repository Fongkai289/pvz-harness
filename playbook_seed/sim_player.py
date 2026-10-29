"""Initial simulator player. All observations are reconstructed from RGB pixels."""


def perceive(image):
    names = ["sunflower", "peashooter", "wallnut", "potato_mine", "cherry_bomb", "snow_pea"]
    colors = [[245, 200, 40], [80, 220, 90], [170, 115, 60],
              [180, 100, 200], [235, 65, 65], [70, 200, 240]]
    cards = []
    for slot in range(6):
        cards.append({"plant": names[slot], "ready": pixel(image, 60 + 19 * slot, 21) == [40, 230, 100]})
    plants = []
    for row in range(5):
        for col in range(9):
            x = 18 + 18 * col
            y = 39 + 18 * row
            best = 0
            plant = None
            for slot in range(6):
                amount = count_color(image, x - 5, y - 5, x + 6, y + 6, colors[slot])
                if amount > 25 and amount > best:
                    best = amount
                    plant = names[slot]
            if plant is not None:
                plants.append({"plant": plant, "row": row, "col": col})
    zombies = []
    for blob in components(image, [210, 70, 145]):
        if blob["area"] >= 8:
            row = int(round((blob["y"] - 39) / 18))
            if row >= 0 and row < 5:
                col = (blob["x"] - 18) / 18
                zombies.append({"row": row, "col": col})
    return {"sun": read_digits(image, 4, 4), "cards": cards, "plants": plants, "zombies": zombies}


def free(observation, row, col):
    return not any(p["row"] == row and p["col"] == col for p in observation["plants"])


def ready(observation, name):
    return any(c["plant"] == name and c["ready"] for c in observation["cards"])


def place(observation, name, rows, cols):
    if not ready(observation, name):
        return None
    for row in rows:
        for col in cols:
            if free(observation, row, col):
                return {"plant": name, "row": row, "col": col}
    return None


def decide(observation, memory):
    plants = observation["plants"]
    zombies = observation["zombies"]
    nearest = [10.0, 10.0, 10.0, 10.0, 10.0]
    flowers = [0, 0, 0, 0, 0]
    shooters = [0, 0, 0, 0, 0]
    walls = [0, 0, 0, 0, 0]
    for z in zombies:
        nearest[z["row"]] = min(nearest[z["row"]], z["col"])
    for p in plants:
        if p["plant"] == "sunflower":
            flowers[p["row"]] += 1
        if p["plant"] in ["peashooter", "snow_pea"]:
            shooters[p["row"]] += 1
        if p["plant"] == "wallnut":
            walls[p["row"]] += 1
    # Stable ordering without hidden game state or future spawn information.
    threat_rows = [pair[1] for pair in sorted([[nearest[r], r] for r in range(5)])]
    economy_rows = [pair[1] for pair in sorted([[flowers[r], r] for r in range(5)])]
    for row in threat_rows:
        if nearest[row] <= 3:
            col = max(0, min(8, int(nearest[row])))
            action = place(observation, "cherry_bomb", [row], [col])
            if action is not None:
                return action
    if sum(flowers) < 5:
        action = place(observation, "sunflower", economy_rows, [0, 1])
        if action is not None:
            return action
    for row in threat_rows:
        if shooters[row] < 1:
            action = place(observation, "peashooter", [row], [2, 1, 3, 4])
            if action is not None:
                return action
    for row in threat_rows:
        if nearest[row] <= 6 and walls[row] == 0:
            col = max(3, min(6, int(nearest[row]) - 1))
            action = place(observation, "wallnut", [row], [col])
            if action is not None:
                return action
    if sum(flowers) < 10 and min(nearest) > 5:
        action = place(observation, "sunflower", economy_rows, [0, 1])
        if action is not None:
            return action
    for row in threat_rows:
        if nearest[row] < 9 and shooters[row] < 2:
            action = place(observation, "peashooter", [row], [2, 3, 4])
            if action is not None:
                return action
    return None
