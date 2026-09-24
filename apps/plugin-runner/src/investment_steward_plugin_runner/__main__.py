from investment_steward_plugin_runner.protocol import handle_line


def main() -> None:
    while True:
        try:
            line = input()
        except EOFError:
            break
        print(handle_line(line), flush=True)


if __name__ == "__main__":
    main()
