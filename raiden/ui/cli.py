# -*- coding: utf-8 -*-
from __future__ import print_function

import sys
import os

import signal
import click
import gevent
from gevent import Greenlet
import gevent.monkey
from ethereum import slogging
from pyethapp.jsonrpc import address_decoder

from raiden.accounts import AccountManager
from raiden.api.rest import APIServer, RestAPI
from raiden.constants import ROPSTEN_REGISTRY_ADDRESS, ROPSTEN_DISCOVERY_ADDRESS
from raiden.network.discovery import ContractDiscovery
from raiden.network.sockfactory import socket_factory
from raiden.settings import (
    INITIAL_PORT,
    DEFAULT_NAT_KEEPALIVE_RETRIES,
)
from raiden.utils import split_endpoint

gevent.monkey.patch_all()


OPTIONS = [
    click.option(
        '--address',
        help=('The ethereum address you would like raiden to use and for which '
              'a keystore file exists in your local system.'),
        default=None,
        type=str,
    ),
    click.option(
        '--keystore-path',
        help=('If you have a non-standard path for the ethereum keystore directory'
              ' provide it using this argument.'),
        default=None,
        type=click.Path(exists=True),
    ),
    click.option(
        '--eth-rpc-endpoint',
        help='"host:port" address of ethereum JSON-RPC server.\n'
        'Also accepts a protocol prefix (http:// or https://) with optional port',
        default='127.0.0.1:8545',  # geth default jsonrpc port
        type=str,
    ),
    click.option(
        '--registry-contract-address',
        help='hex encoded address of the registry contract.',
        default=ROPSTEN_REGISTRY_ADDRESS,  # testnet default
        type=str,
    ),
    click.option(
        '--discovery-contract-address',
        help='hex encoded address of the discovery contract.',
        default=ROPSTEN_DISCOVERY_ADDRESS,  # testnet default
        type=str,
    ),
    click.option(
        '--listen-address',
        help='"host:port" for the raiden service to listen on.',
        default="0.0.0.0:{}".format(INITIAL_PORT),
        type=str,
    ),
    click.option(
        '--rpccorsdomain',
        help='Comma separated list of domains to accept cross origin requests. \n'
        '(localhost enabled by default)',
        default="http://localhost:*/*",
        type=str,
    ),
    click.option(
        '--logging',
        help='ethereum.slogging config-string (\'<logger1>:<level>,<logger2>:<level>\')',
        default=':INFO',
        type=str,
    ),
    click.option(
        '--logfile',
        help='file path for logging to file',
        default=None,
        type=str,
    ),
    click.option(
        '--max-unresponsive-time',
        help=(
            'Max time in seconds for which an address can send no packets and '
            'still be considered healthy.'
        ),
        default=120,
        type=int,
    ),
    click.option(
        '--send-ping-time',
        help=(
            'Time in seconds after which if we have received no message from a '
            'node we have a connection with, we are going to send a PING message'
        ),
        default=60,
        type=int,
    ),
    click.option(
        '--console/--no-console',
        help=(
            'Start with or without the command line interface. Defualt is to '
            'start with the CLI disabled'
        ),
        default=False,
    ),
    click.option(
        '--rpc/--no-rpc',
        help=(
            'Start with or without the RPC server. Default is to start '
            'the RPC server'
        ),
        default=True,
    ),
    click.option(
        '--api-address',
        help='"host:port" for the RPC server to listen on.',
        default="127.0.0.1:5001",
        type=str,
    ),
    click.option(
        '--password-file',
        help='Text file containing password for provided account',
        default=None,
        type=click.File(lazy=True),
    ),
]


def options(func):
    """Having the common app options as a decorator facilitates reuse.
    """
    for option in OPTIONS:
        func = option(func)
    return func


@options
@click.command()
def app(address,
        keystore_path,
        eth_rpc_endpoint,
        registry_contract_address,
        discovery_contract_address,
        listen_address,
        rpccorsdomain,  # pylint: disable=unused-argument
        socket,
        logging,
        logfile,
        max_unresponsive_time,
        send_ping_time,
        api_address,
        rpc,
        console,
        password_file):

    from raiden.app import App
    from raiden.network.rpc.client import BlockChainService

    slogging.configure(logging, log_file=logfile)

    # config_file = args.config_file
    (listen_host, listen_port) = split_endpoint(listen_address)
    (api_host, api_port) = split_endpoint(api_address)

    config = App.DEFAULT_CONFIG.copy()
    config['host'] = listen_host
    config['port'] = listen_port
    config['console'] = console
    config['rpc'] = rpc
    config['api_host'] = api_host
    config['api_port'] = api_port
    config['socket'] = socket

    retries = max_unresponsive_time / DEFAULT_NAT_KEEPALIVE_RETRIES
    config['protocol']['nat_keepalive_retries'] = retries
    config['protocol']['nat_keepalive_timeout'] = send_ping_time

    accmgr = AccountManager(keystore_path)
    if not accmgr.accounts:
        raise RuntimeError('No Ethereum accounts found in the user\'s system')

    if not accmgr.address_in_keystore(address):
        addresses = list(accmgr.accounts.keys())
        formatted_addresses = [
            '[{:3d}] - 0x{}'.format(idx, addr)
            for idx, addr in enumerate(addresses)
        ]

        should_prompt = True

        print('The following accounts were found in your machine:')
        print('')
        print('\n'.join(formatted_addresses))
        print('')

        while should_prompt:
            idx = click.prompt('Select one of them by index to continue', type=int)

            if idx >= 0 and idx < len(addresses):
                should_prompt = False
            else:
                print("\nError: Provided index '{}' is out of bounds\n".format(idx))

        address = addresses[idx]

    password = None
    if password_file:
        password = password_file.read().splitlines()[0]
    if password:
        try:
            privatekey_bin = accmgr.get_privkey(address, password)
        except ValueError as e:
            # ValueError exception raised if the password is incorrect
            print('Incorret password for {} in file. Aborting ...'.format(address))
            sys.exit(1)
    else:
        unlock_tries = 3
        while True:
            try:
                privatekey_bin = accmgr.get_privkey(address)
                break
            except ValueError as e:
                # ValueError exception raised if the password is incorrect
                if unlock_tries == 0:
                    print(
                        'Exhausted passphrase unlock attempts for {}. Aborting ...'.format(address)
                    )
                    sys.exit(1)

                print(
                    'Incorrect passphrase to unlock the private key. {} tries remaining. '
                    'Please try again or kill the process to quit. '
                    'Usually Ctrl-c.'.format(unlock_tries)
                )
                unlock_tries -= 1

    privatekey_hex = privatekey_bin.encode('hex')
    config['privatekey_hex'] = privatekey_hex

    endpoint = eth_rpc_endpoint

    if eth_rpc_endpoint.startswith("http://"):
        endpoint = eth_rpc_endpoint[len("http://"):]
        rpc_port = 80
    elif eth_rpc_endpoint.startswith("https://"):
        endpoint = eth_rpc_endpoint[len("https://"):]
        rpc_port = 443

    if ':' not in endpoint:  # no port was given in url
        rpc_host = endpoint
    else:
        rpc_host, rpc_port = split_endpoint(endpoint)

    # user may have provided registry and discovery contracts with leading 0x
    registry_contract_address = address_decoder(registry_contract_address)
    discovery_contract_address = address_decoder(discovery_contract_address)

    try:
        blockchain_service = BlockChainService(
            privatekey_bin,
            registry_contract_address,
            host=rpc_host,
            port=rpc_port,
        )
    except ValueError as e:
        # ValueError exception raised if:
        # - The registry contract address doesn't have code, this might happen
        # if the connected geth process is not synced or if the wrong address
        # is provided (e.g. using the address from a smart contract deployed on
        # ropsten with a geth node connected to morden)
        print(e.message)
        sys.exit(1)

    discovery = ContractDiscovery(
        blockchain_service.node_address,
        blockchain_service.discovery(discovery_contract_address)
    )

    # default database directory
    raiden_directory = os.path.join(os.path.expanduser('~'), '.raiden')
    if not os.path.exists(raiden_directory):
        os.makedirs(raiden_directory)
    database_path = os.path.join(raiden_directory, 'log.db')
    config['database_path'] = database_path

    return App(config, blockchain_service, discovery)


@options
@click.command()
@click.pass_context
def run(ctx, **kwargs):
    from raiden.api.python import RaidenAPI
    from raiden.ui.console import Console

    # TODO:
    # - Ask for confirmation to quit if there are any locked transfers that did
    # not timeout.
    (listen_host, listen_port) = split_endpoint(kwargs['listen_address'])
    with socket_factory(listen_host, listen_port) as mapped_socket:
        kwargs['socket'] = mapped_socket.socket

        app_ = ctx.invoke(app, **kwargs)

        # spawn address registration to avoid block while waiting for the next block
        registry_event = gevent.spawn(
            app_.discovery.register,
            app_.raiden.address,
            mapped_socket.external_ip,
            mapped_socket.external_port,
        )
        app_.raiden.register_registry(app_.raiden.chain.default_registry.address)

        domain_list = []
        if kwargs['rpccorsdomain']:
            if ',' in kwargs['rpccorsdomain']:
                for domain in kwargs['rpccorsdomain'].split(','):
                    domain_list.append(str(domain))
            else:
                domain_list.append(str(kwargs['rpccorsdomain']))

        if ctx.params['rpc']:
            raiden_api = RaidenAPI(app_.raiden)
            rest_api = RestAPI(raiden_api)
            api_server = APIServer(rest_api, cors_domain_list=domain_list)
            (api_host, api_port) = split_endpoint(kwargs["api_address"])

            Greenlet.spawn(
                api_server.run,
                api_host,
                api_port,
                debug=False,
                use_evalex=False
            )

            print(
                "The Raiden API RPC server is now running at http://{}:{}/.\n\n"
                "See the Raiden documentation for all available endpoints at\n"
                "https://github.com/raiden-network/raiden/blob/master/docs/Rest-Api.rst".format(
                    api_host,
                    api_port,
                )
            )

        if ctx.params['console']:
            console = Console(app_)
            console.start()

        registry_event.join()
        # wait for interrupt
        event = gevent.event.Event()
        gevent.signal(signal.SIGQUIT, event.set)
        gevent.signal(signal.SIGTERM, event.set)
        gevent.signal(signal.SIGINT, event.set)
        event.wait()

        app_.stop(graceful=True)
