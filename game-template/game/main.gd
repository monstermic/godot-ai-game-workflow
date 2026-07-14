extends Node2D

var state := ReferenceGameState.new()

func _process(delta: float) -> void:
    if Input.is_key_pressed(KEY_R) and (state.completed or state.failed):
        state.restart()
    var direction := Vector2(
        float(Input.is_key_pressed(KEY_D) or Input.is_key_pressed(KEY_RIGHT)) - float(Input.is_key_pressed(KEY_A) or Input.is_key_pressed(KEY_LEFT)),
        float(Input.is_key_pressed(KEY_S) or Input.is_key_pressed(KEY_DOWN)) - float(Input.is_key_pressed(KEY_W) or Input.is_key_pressed(KEY_UP))
    )
    state.move_player(direction, delta)
    state.evaluate_collisions()
    queue_redraw()

func _draw() -> void:
    draw_rect(ReferenceGameState.ARENA, Color("172033"))
    draw_circle(state.goal_position, 24, Color("f7c948"))
    draw_circle(state.enemy_position, 24, Color("ef476f"))
    draw_rect(Rect2(state.player_position - Vector2(16, 16), Vector2(32, 32)), Color("55d6be"))
    var message := "Reach gold · avoid red · WASD/Arrows | Lives %d | Score %d" % [state.lives, state.score]
    if state.completed:
        message = "You win! Reward +100 · Press R to restart"
    elif state.failed:
        message = "Game over · Press R to restart"
    draw_string(ThemeDB.fallback_font, Vector2(24, 36), message, HORIZONTAL_ALIGNMENT_LEFT, -1, 22, Color.WHITE)
