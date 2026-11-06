import argparse
from .core import Chat, Message, Grant, Policy, SyntheticAdapter, Gateway, Request, CommandRouter


def main():
    parser = argparse.ArgumentParser(description='Synthetic-only gateway; live Telegram disabled.')
    parser.add_argument('command', choices=['demo'], help='Exercise read and explicit reply-aware /save with fixtures')
    parser.parse_args()
    adapter = SyntheticAdapter({'c': Chat('c'), 'self': Chat('self')}, {
        ('c', '1'): Message('1', text='synthetic fixture'),
        ('c', '2'): Message('2', reply_to='1')})
    policy = Policy([Grant('fixture', 'a', 'read', frozenset({'c'})),
                     Grant('fixture', 'a', 'write', frozenset({'self'}))])
    gateway = Gateway({'a': adapter}, policy)
    print(gateway.execute('fixture', Request('a', 'read', 'c', '1')))
    print(CommandRouter(gateway).invoke('fixture', '/save', 'a', 'c', '2'))


if __name__ == '__main__':
    main()
