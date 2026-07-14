class_name ReferenceGameState
extends RefCounted

const SPEED := 240.0
const START := Vector2(96, 270)
const ARENA := Rect2(32, 64, 896, 444)

var player_position := START
var goal_position := Vector2(864, 270)
var enemy_position := Vector2(480, 270)
var lives := 3
var score := 0
var completed := false
var failed := false

func move_player(direction: Vector2, delta: float) -> void:
    if completed or failed:
        return
    player_position += direction.normalized() * SPEED * delta
    player_position.x = clampf(player_position.x, ARENA.position.x, ARENA.end.x)
    player_position.y = clampf(player_position.y, ARENA.position.y, ARENA.end.y)

func evaluate_collisions() -> void:
    if completed or failed:
        return
    if player_position.distance_to(goal_position) <= 28.0:
        completed = true
        score += 100
        return
    if player_position.distance_to(enemy_position) <= 28.0:
        lives -= 1
        if lives <= 0:
            failed = true
        else:
            player_position = START

func restart() -> void:
    player_position = START
    lives = 3
    score = 0
    completed = false
    failed = false
