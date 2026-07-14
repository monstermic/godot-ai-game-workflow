extends SceneTree

var failures := 0

func expect(condition: bool, message: String) -> void:
    if not condition:
        failures += 1
        push_error(message)

func _initialize() -> void:
    var MainScene = load("res://game/main.tscn")
    expect(MainScene != null, "main scene loads")
    if MainScene != null:
        var main = MainScene.instantiate()
        expect(main != null, "main scene instantiates")
        main.free()
    var GameState = load("res://game/game_state.gd")
    expect(GameState != null, "game state script loads")
    if GameState != null:
        var state = GameState.new()
        var start = state.player_position
        state.move_player(Vector2.RIGHT, 0.5)
        expect(state.player_position.x > start.x, "player moves right")
        state.player_position = state.goal_position
        state.evaluate_collisions()
        expect(state.completed, "touching goal wins")
        expect(state.score == 100, "winning grants reward")
        state.restart()
        expect(not state.completed and state.score == 0, "restart resets win and reward")
        state.lives = 1
        state.player_position = state.enemy_position
        state.evaluate_collisions()
        expect(state.failed, "last enemy collision loses")
    print("AIGAME_TESTS failures=%d" % failures)
    quit(failures)
