#!/bin/python3
import datetime
import json
import pathlib

import substrateinterface
import argparse
import logging
import enum
import os


class ChainMajorVersion(enum.Enum):
    PRE_12_MAJOR_VERSION = 65,
    AT_LEAST_12_MAJOR_VERSION = 68

    @classmethod
    def from_spec_version(cls, spec_version):
        return cls(ChainMajorVersion.PRE_12_MAJOR_VERSION if spec_version <= 65
                   else ChainMajorVersion.AT_LEAST_12_MAJOR_VERSION)


def get_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="""
Script for maintenance operations on AlephNode chain with regards to pallet balances.

It has following functionality: 
* workaround for bug https://github.com/paritytech/polkadot-sdk/pull/2700/files, that make sure all 
  accounts have at least ED as their free balance,
* programmatic support for sending Balances.UpgradeAccounts ext for all accounts,
* checking pallet balances and account reference counters invariants.

By default, it connects to a AlephZero Testnet and performs sanity checks only ie not changing state of the chain at all.
Accounts that do not satisfy those checks are written to accounts-with-failed-invariants.json file.
""",
        formatter_class=argparse.RawTextHelpFormatter)

    parser.add_argument('--ws-url',
                        type=str,
                        default='wss://ws.test.azero.dev:443',
                        help='WS URL of the RPC node to connect to. Default is wss://ws.test.azero.dev:443')
    parser.add_argument('--log-level',
                        default='info',
                        choices=['debug', 'info', 'warning', 'error'],
                        help='Provide logging level. Default is info')
    parser.add_argument('--dry-run',
                        action='store_true',
                        help='Specify this switch if script should just print what if would do. Default: False')
    parser.add_argument('--transfer-calls-in-batch',
                        type=int,
                        default=64,
                        help='How many transfer calls to perform in a single batch transaction. Default: 64')
    parser.add_argument('--upgrade-accounts-in-batch',
                        type=int,
                        default=128,
                        help='How many accounts to upgrade in a single transaction. Default: 128')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--fix-free-balance',
                        action='store_true',
                        help='Specify this switch if script should find all accounts '
                             'that have free < ED and send transfers so that free >= ED. '
                             'It requires AlephNode < 12.X version and SENDER_ACCOUNT env to be set with '
                             'a mnemonic phrase of sender account that has funds for transfers and fees. '
                             'dust-accounts.json file is saved with all such accounts.'
                             'Must be used exclusively with --upgrade-accounts. '
                             'Default: False')
    group.add_argument('--upgrade-accounts',
                        action='store_true',
                        help='Specify this switch if script should send Balances.UpgradeAccounts '
                             'for all accounts on a chain. It requires at least AlephNode 12.X version '
                             'and SENDER_ACCOUNT env to be set with a mnemonic phrase of sender account that has funds '
                             'for transfers and fees.'
                             'Must be used exclusively with --fix-free-balance. '
                             'Default: False')

    return parser.parse_args()


def get_chain_major_version(chain_connection):
    """
    Retrieves spec_version from chain and returns an enum whether this is pre 12 version or at least 12 version
    :param chain_connection: WS handler
    :return: ChainMajorVersion
    """
    runtime_version = chain_connection.get_block_runtime_version(None)
    spec_version = runtime_version['specVersion']
    major_version = ChainMajorVersion.from_spec_version(spec_version)
    return major_version


def check_account_invariants(account, chain_major_version, ed):
    """
    This predicate checks whather an accounts meet pallet balances and account reference counters predicates.

    :param account: AccountInfo struct (element of System.Accounts StorageMap)
    :param chain_major_version: integer which is major version of AlephNode chain
    :param ed: existential deposit
    :return: True if account meets all invariants, False otherwise
    """
    providers = account['providers'].value
    consumers = account['consumers'].value
    free = account['data']['free'].value
    reserved = account['data']['reserved'].value

    # in both versions, consumers must be 0 if providers are 0; also there is only one provider which is pallet
    # balance so max possible value of providers is 1
    account_ref_counter_invariant = (providers <= 1 and consumers == 0) or (consumers > 0 and providers == 1)

    if chain_major_version == ChainMajorVersion.PRE_12_MAJOR_VERSION:
        misc_frozen = account['data']['misc_frozen'].value
        fee_frozen = account['data']['fee_frozen'].value

        # in pre-12 version, existential deposit applies to total balance
        ed_is_for_total_balance_invariant = free + reserved >= ed

        # in pre-12 version, locked balance applies only to free balance
        locked_balance_is_on_free_balance_invariant = free >= max(misc_frozen, fee_frozen)

        return account_ref_counter_invariant and \
               ed_is_for_total_balance_invariant and \
               locked_balance_is_on_free_balance_invariant

    frozen = account['data']['frozen'].value
    flags = account['data']['flags'].value

    # in at least 12 version, ED must be available on free balance for account to exist
    ed_is_for_free_balance_only_invariant = free >= ed

    # in at least 12 version, locked balance applies to total balance
    locked_balance_is_on_total_balance_invariant = free + reserved >= frozen

    is_account_already_upgraded = flags >= 2 ** 127
    # the reasons we check if account is upgraded only in this check and not in the previous invariants is that
    # * ed_is_for_free_balance_only_invariant is stricter than ed_is_for_total_balance_invariant and account
    #   account upgrade code has a bug so it does not provide ed for accounts which does not meet this in 11 version
    # * locked_balance_is_on_total_balance_invariant is less strict than locked_balance_is_on_free_balance_invariant
    # * consumer_ref_applies_to_suspended_balances_invariant applies to both versions
    consumer_ref_applies_to_suspended_balances_invariant = \
        (not is_account_already_upgraded or (frozen == 0 and reserved == 0) or consumers > 0)
    return \
        account_ref_counter_invariant and \
        ed_is_for_free_balance_only_invariant and \
        locked_balance_is_on_total_balance_invariant and \
        consumer_ref_applies_to_suspended_balances_invariant


def filter_false_accounts(chain_connection,
                          ed,
                          chain_major_version,
                          check_accounts_predicate,
                          check_accounts_predicate_name=""):
    """
    Filters our accounts from the list on which predicate returns False
    :param chain_connection: WS handler
    :param ed: existential deposit
    :param chain_major_version: enum ChainMajorVersion
    :param check_accounts_predicate: a function that takes three arguments predicate(account, chain_major_version, ed)
    :param check_accounts_predicate_name: name of the predicate, used for logging reasons only
    :return: a list which has those chain accounts which returns False on check_accounts_predicate
    """
    accounts_that_do_not_meet_predicate = []
    account_query = chain_connection.query_map('System', 'Account', page_size=1000)
    total_accounts_count = 0

    for (i, (account_id, info)) in enumerate(account_query):
        total_accounts_count += 1
        if not check_accounts_predicate(info, chain_major_version, ed):
            log.debug(f"Account {account_id.value} does not meet given predicate!"
                      f" Check name: {check_accounts_predicate_name}!")
            accounts_that_do_not_meet_predicate.append([account_id.value, info.serialize()])
        if i % 5000 == 0 and i > 0:
            log.info(f"Checked {i} accounts")

    log.info(f"Total accounts that match given predicate {check_accounts_predicate_name} is {total_accounts_count}")
    return accounts_that_do_not_meet_predicate


def check_if_account_would_be_dust_in_12_version(account, chain_major_version, ed):
    """
    This predicate checks if a valid account in pre-12 version will be invalid in version 12.

    :param account: AccountInfo struct (element of System.Accounts StorageMap)
    :param chain_major_version: Must be < 12
    :param ed: existential deposit
    :return: True if account free balance < ED, False otherwise
    """

    assert chain_major_version == ChainMajorVersion.PRE_12_MAJOR_VERSION, \
        "Chain major version must be less than 12!"
    assert check_account_invariants(account, chain_major_version, ed), \
        f"Account {account} does not meet pre-12 version invariants!"

    free = account['data']['free'].value

    return free < ed


def find_dust_accounts(chain_connection, ed, chain_major_version):
    """
    This function finds all accounts that are valid in 11 version, but not on 12 version
    """
    assert chain_major_version == ChainMajorVersion.PRE_12_MAJOR_VERSION, \
        "Chain major version must be less than 12!"
    return filter_false_accounts(chain_connection=chain_connection,
                                 ed=ed,
                                 chain_major_version=chain_major_version,
                                 check_accounts_predicate=
                                 lambda x, y, z: not check_if_account_would_be_dust_in_12_version(x, y, z),
                                 check_accounts_predicate_name="\'account valid in pre-12 version but not in 12 "
                                                               "version\'")


def format_balance(chain_connection, amount):
    """
    Helper method to display underlying U128 Balance type in human-readable form
    :param chain_connection: WS connection handler (for retrieving token symbol metadata)
    :param amount: ammount to be formatted
    :return: balance in human-readable form
    """
    decimals = chain_connection.token_decimals or 12
    amount = format(amount / 10 ** decimals)
    token = chain_connection.token_symbol
    return f"{amount} {token}"


def batch_transfer(chain_connection,
                   input_args,
                   accounts,
                   amount,
                   sender_keypair):
    """
    Send Balance.Transfer calls in a batch
    :param chain_connection: WS connection handler
    :param input_args: script input arguments returned from argparse
    :param accounts: transfer beneficents
    :param amount: amount to be transferred
    :param sender_keypair: keypair of sender account
    :return: None. Can raise exception in case of substrateinterface.SubstrateRequestException thrown
    """
    for (i, account_ids_chunk) in enumerate(chunks(accounts, input_args.transfer_calls_in_batch)):
        balance_calls = list(map(lambda account: chain_connection.compose_call(
            call_module='Balances',
            call_function='transfer',
            call_params={
                'dest': account,
                'value': amount,
            }), account_ids_chunk))
        batch_call = chain_connection.compose_call(
            call_module='Utility',
            call_function='batch',
            call_params={
                'calls': balance_calls
            }
        )

        extrinsic = chain_connection.create_signed_extrinsic(call=batch_call, keypair=sender_keypair)
        log.info(f"About to send {len(balance_calls)} transfers, each with {format_balance(chain_connection, amount)} "
                 f"from {sender_keypair.ss58_address} to below accounts: "
                 f"{account_ids_chunk}")

        submit_extrinsic(chain_connection, extrinsic, len(balance_calls), args.dry_run)


def submit_extrinsic(chain_connection,
                     extrinsic,
                     expected_number_of_events,
                     dry_run):
    """
    Submit a signed extrinsic
    :param chain_connection: WS connection handler
    :param extrinsic: an ext to be sent
    :param expected_number_of_events: how many events caller expects to be emitted from chain
    :param dry_run: boolean whether to actually send ext or not
    :return: None. Can raise exception in case of substrateinterface.SubstrateRequestException thrown
    """
    try:
        log.debug(f"Extrinsic to be sent: {extrinsic}")
        if not dry_run:
            receipt = chain_connection.submit_extrinsic(extrinsic, wait_for_inclusion=True)
            log.info(f"Extrinsic included in block {receipt.block_hash}: "
                     f"Paid {format_balance(chain_connection, receipt.total_fee_amount)}")
            if receipt.is_success:
                log.debug("Extrinsic success.")
                if len(receipt.triggered_events) < expected_number_of_events:
                    log.debug(
                        f"Emitted fewer events than expected: "
                        f"{len(receipt.triggered_events)} < {expected_number_of_events}")
            else:
                log.warning(f"Extrinsic failed with following message: {receipt.error_message}")
        else:
            log.info(f"Not sending extrinsic, --dry-run is enabled.")
    except substrateinterface.SubstrateRequestException as e:
        log.warning(f"Failed to submit extrinsic: {e}")
        raise e


def upgrade_accounts(chain_connection,
                     input_args,
                     ed,
                     chain_major_version,
                     sender_keypair):
    """
    Prepare and send Balances.UpgradeAccounts call for all accounts on a chain
    :param chain_connection: WS connection handler
    :param input_args: script input arguments returned from argparse
    :param ed: chain existential deposit
    :param chain_major_version: enum ChainMajorVersion
    :param sender_keypair: keypair of sender account
    :return: None. Can raise exception in case of substrateinterface.SubstrateRequestException thrown
    """
    log.info("Querying all accounts.")
    all_accounts_on_chain = list(map(lambda x: x[0], filter_false_accounts(chain_connection,
                                                                           ed,
                                                                           chain_major_version,
                                                                           lambda x, y, z: False,
                                                                           "\'all accounts\'")))

    for (i, account_ids_chunk) in enumerate(chunks(all_accounts_on_chain, input_args.upgrade_accounts_in_batch)):
        upgrade_accounts_call = chain_connection.compose_call(
            call_module='Balances',
            call_function='upgrade_accounts',
            call_params={
                'who': account_ids_chunk,
            }
        )

        extrinsic = chain_connection.create_signed_extrinsic(call=upgrade_accounts_call, keypair=sender_keypair)
        log.info(
            f"About to upgrade {len(account_ids_chunk)} accounts, each with "
            f"{format_balance(chain_connection, existential_deposit)}")

        submit_extrinsic(chain_connection, extrinsic, len(account_ids_chunk), args.dry_run)


def chunks(list_of_elements, n):
    """
    Lazily split 'list_of_elements' into 'n'-sized chunks.
    """
    for i in range(0, len(list_of_elements), n):
        yield list_of_elements[i:i + n]


def perform_account_sanity_checks(chain_connection,
                                  ed,
                                  chain_major_version):
    """
    Checks whether all accounts on a chain matches pallet balances invariants
    :param chain_connection: WS connection handler
    :param ed: chain existential deposit
    :param chain_major_version: enum ChainMajorVersion
    :return:None
    """
    invalid_accounts = filter_false_accounts(chain_connection=chain_connection,
                                             ed=ed,
                                             chain_major_version=chain_major_version,
                                             check_accounts_predicate=check_account_invariants,
                                             check_accounts_predicate_name="\'account invariants\'")
    if len(invalid_accounts) > 0:
        log.warning(f"Found {len(invalid_accounts)} accounts that do not meet balances invariants!")
        save_accounts_to_json_file("accounts-with-failed-invariants.json", invalid_accounts)
    else:
        log.info(f"All accounts on chain {chain_connection.chain} meet balances invariants.")


def save_accounts_to_json_file(json_file_name, accounts):
    with open(json_file_name, 'w') as f:
        json.dump(accounts, f)
        log.info(f"Wrote file '{json_file_name}'")


def get_global_logger(input_args):
    time_now = datetime.datetime.now().strftime("%d-%m-%Y_%H:%M:%S")
    script_name_without_extension = pathlib.Path(__file__).stem
    logging.basicConfig(
        level=input_args.log_level.upper(),
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(f"{script_name_without_extension}-{time_now}.log"),
            logging.StreamHandler()
        ]
    )
    return logging


if __name__ == "__main__":
    args = get_args()
    log = get_global_logger(args)

    if args.fix_free_balance or args.upgrade_accounts:
        sender_origin_account_seed = os.getenv('SENDER_ACCOUNT')
        if sender_origin_account_seed is None:
            log.error(f"When specifying --fix-free-balance or --upgrade-accounts, env SENDER_ACCOUNT must exists. "
                      f"Exiting.")
            exit(1)
    if args.dry_run:
        log.info(f"Dry-run mode is enabled.")

    chain_ws_connection = substrateinterface.SubstrateInterface(args.ws_url)
    log.info(f"Connected to {chain_ws_connection.name}: {chain_ws_connection.chain} {chain_ws_connection.version}")

    chain_major_version = get_chain_major_version(chain_ws_connection)
    log.info(f"Major version of chain connected to is {chain_major_version}")
    if args.fix_free_balance:
        if chain_major_version is not ChainMajorVersion.PRE_12_MAJOR_VERSION:
            log.error(f"--fix-free-balance can be used only on chains with pre-12 version. Exiting.")
            exit(2)
    if args.upgrade_accounts:
        if chain_major_version is not ChainMajorVersion.AT_LEAST_12_MAJOR_VERSION:
            log.error(f"--upgrade-accounts can be used only on chains with at least 12 version. Exiting.")
            exit(3)

    existential_deposit = chain_ws_connection.get_constant("Balances", "ExistentialDeposit").value
    log.info(f"Existential deposit is {format_balance(chain_ws_connection, existential_deposit)}")

    if args.fix_free_balance:
        sender_origin_account_keypair = substrateinterface.Keypair.create_from_uri(sender_origin_account_seed)
        log.info(f"Using following account for transfers: {sender_origin_account_keypair.ss58_address}")
        log.info(f"Will send at most {args.transfer_calls_in_batch} transfers in a batch.")
        log.info(f"Looking for accounts that would be dust in 12 version.")
        dust_accounts_in_12_version = find_dust_accounts(chain_ws_connection, existential_deposit, chain_major_version)
        if len(dust_accounts_in_12_version):
            log.info(f"Found {len(dust_accounts_in_12_version)} accounts that will be invalid in 12 version.")
            save_accounts_to_json_file("dust-accounts.json", dust_accounts_in_12_version)
            log.info("Adjusting balances by sending transfers.")
            batch_transfer(chain_connection=chain_ws_connection,
                           input_args=args,
                           accounts=list(map(lambda x: x[0], dust_accounts_in_12_version)),
                           amount=existential_deposit,
                           sender_keypair=sender_origin_account_keypair)
            log.info(f"Transfers done.")
        else:
            log.info(f"No dust accounts found, skipping transfers.")
    if args.upgrade_accounts:
        sender_origin_account_keypair = substrateinterface.Keypair.create_from_uri(sender_origin_account_seed)
        log.info(f"Using following account for upgrade_accounts: {sender_origin_account_keypair.ss58_address}")
        log.info(f"Will upgrade at most {args.upgrade_accounts_in_batch} accounts in a batch.")
        upgrade_accounts(chain_connection=chain_ws_connection,
                         input_args=args,
                         ed=existential_deposit,
                         chain_major_version=chain_major_version,
                         sender_keypair=sender_origin_account_keypair)
        log.info("Upgrade accounts done.")

    log.info(f"Performing pallet balances sanity checks.")
    perform_account_sanity_checks(chain_connection=chain_ws_connection,
                                  ed=existential_deposit,
                                  chain_major_version=chain_major_version)
    log.info(f"DONE")
