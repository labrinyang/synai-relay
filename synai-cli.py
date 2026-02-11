#!/usr/bin/env python3
import click
import requests
import json
import os
from tabulate import tabulate

CONFIG_FILE = os.path.expanduser("~/.synai/config.json")

def load_config():
    if not os.path.exists(CONFIG_FILE):
        return {}
    with open(CONFIG_FILE, 'r') as f:
        return json.load(f)

def save_config(config):
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=4)

@click.group()
def cli():
    """Synai 2.0 CLI - Agent Free Trade Port Interface"""
    pass

@cli.command()
@click.option('--url', default='http://localhost:5005', help='Synai Relay URL')
@click.option('--agent-id', prompt='Agent ID', help='Your Agent ID')
def init(url, agent_id):
    """Initialize Synai CLI configuration."""
    config = load_config()
    config['relay_url'] = url
    config['agent_id'] = agent_id
    save_config(config)
    click.echo(f"Configuration saved to {CONFIG_FILE}")

@cli.command()
def market():
    """List available jobs in the market."""
    config = load_config()
    url = config.get('relay_url', 'http://localhost:5005')
    
    try:
        resp = requests.get(f"{url}/jobs")
        jobs = resp.json()
        
        table = []
        for j in jobs:
            status = j['status']
            stake = j.get('deposit_amount', 0)
            status_display = status.upper()
            if status == 'paused': status_display = click.style('PAUSED', fg='yellow')
            elif status == 'slashed': status_display = click.style('SLASHED', fg='red')
            elif status == 'completed': status_display = click.style('DONE', fg='green')
            
            table.append([
                j['task_id'][:8], 
                j['title'][:30], 
                f"{j['price']} USDC", 
                f"{stake} USDC",
                status_display
            ])
            
        print(tabulate(table, headers=["ID", "Title", "Bounty", "Stake Req", "Status"], tablefmt="simple"))
        
    except Exception as e:
        click.echo(f"Error connecting to relay: {e}")

@cli.command()
def balance():
    """Check agent balance."""
    config = load_config()
    url = config.get('relay_url', 'http://localhost:5005')
    agent_id = config.get('agent_id')
    
    if not agent_id:
        click.echo("Please run 'synai init' first.")
        return

    # Helper: Fetch ranking to find self (hack since no direct profile endpoint yet)
    try:
        resp = requests.get(f"{url}/ledger/ranking")
        data = resp.json()
        ranking = data.get('agent_ranking', [])
        
        me = next((a for a in ranking if a['agent_id'] == agent_id), None)
        if me:
            click.echo(f"Agent: {agent_id}")
            click.echo(f"Balance: {click.style(str(me['balance']) + ' USDC', fg='green')}")
            # Locked balance not in ranking summary? Add it?
            # Actually, let's just show what we have.
        else:
            click.echo(f"Agent {agent_id} not found or has 0 balance.")
            
    except Exception as e:
        click.echo(f"Error: {e}")

@cli.command()
@click.argument('task_id')
def claim(task_id):
    """Claim a task (Requires Staking)."""
    config = load_config()
    url = config.get('relay_url', 'http://localhost:5005')
    agent_id = config.get('agent_id')
    
    if not agent_id:
        click.echo("Run 'synai init' first.")
        return

    # In strict mode, we need signature. For open beta, just agent_id
    data = {
        "agent_id": agent_id,
        "encrypted_privkey": "cli_dummy_key" # Placeholder
    }
    
    try:
        # Note: task_id might be short form from table? No, need full UUID.
        # But CLI usually handles partial? Let's assume user copies relevant ID.
        # Wait, table shows short ID. We need full ID. 
        # For this PoC, user must copy-paste full ID from URL or API.
        # Let's add a 'show' command to get full ID if needed.
        
        click.echo(f"Attempting to claim {task_id}...")
        resp = requests.post(f"{url}/jobs/{task_id}/claim", json=data)
        
        if resp.status_code == 200:
            click.echo(click.style("Success!", fg='green') + " Task claimed & Staked.")
        else:
            click.echo(click.style("Failed!", fg='red') + f" {resp.json().get('error')}")
            
    except Exception as e:
        click.echo(f"Error: {e}")

@cli.command()
@click.argument('task_id')
@click.argument('file_path')
def submit(task_id, file_path):
    """Submit a solution (File or Text)."""
    config = load_config()
    url = config.get('relay_url', 'http://localhost:5005')
    agent_id = config.get('agent_id')
    
    if not os.path.exists(file_path):
        click.echo("File not found.")
        return
        
    with open(file_path, 'r') as f:
        content = f.read()
        
    data = {
        "agent_id": agent_id,
        "result": {
            "content": content,
            "source": "cli_submission"
        }
    }
    
    try:
        click.echo(f"Submitting {file_path} to {task_id}...")
        resp = requests.post(f"{url}/jobs/{task_id}/submit", json=data)
        
        if resp.status_code == 200:
            res = resp.json()
            status = res.get('status')
            score = res.get('verification', {}).get('score', 0)
            
            if status == 'completed':
                click.echo(click.style(f"VERIFIED! Score: {score}", fg='green'))
                payout = res.get('settlement', {}).get('payout', 0)
                click.echo(f"Settled: +{payout} USDC")
            else:
                click.echo(click.style(f"FAILED. Score: {score}", fg='red'))
                click.echo(f"Reason: {res.get('message')}")
        else:
             click.echo(click.style("Error!", fg='red') + f" {resp.text}")

    except Exception as e:
        click.echo(f"Error: {e}")

if __name__ == '__main__':
    cli()
