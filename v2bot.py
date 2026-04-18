import discord
from discord.ext import commands, tasks
import asyncio
import subprocess
import json
from datetime import datetime
import shlex
import logging
import shutil
import os
import random
import string
from typing import Optional, List, Dict, Any
import threading
import time

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger('vps_bot')

# Check if docker command is available
if not shutil.which("docker"):
    logger.error("Docker command not found. Please ensure Docker is installed.")
    raise SystemExit("Docker command not found. Please ensure Docker is installed.")

# Bot setup
intents = discord.Intents.default()
intents.messages = True
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix='!', intents=intents, help_command=None)

# Main admin user ID
MAIN_ADMIN_ID = 1416491351108878417
# VPS User Role ID
VPS_USER_ROLE_ID = 1431499643698544720
# Docker image to use for VPS containers
DOCKER_IMAGE = "ubuntu:22.04"
# SSH port range for containers
SSH_PORT_START = 10000

# CPU monitoring settings
CPU_THRESHOLD = 90
CHECK_INTERVAL = 60
cpu_monitor_active = True

# ─── Data storage ──────────────────────────────────────────────────────────────

def load_data():
    try:
        with open('user_data.json', 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        logger.warning("user_data.json not found or corrupted, initializing empty data")
        return {}

def load_vps_data():
    try:
        with open('vps_data.json', 'r') as f:
            loaded = json.load(f)
            vps_data = {}
            for uid, v in loaded.items():
                if isinstance(v, dict):
                    if "container_name" in v:
                        vps_data[uid] = [v]
                    else:
                        vps_data[uid] = list(v.values())
                elif isinstance(v, list):
                    vps_data[uid] = v
                else:
                    logger.warning(f"Unknown VPS data format for user {uid}, skipping")
                    continue
            return vps_data
    except (FileNotFoundError, json.JSONDecodeError):
        logger.warning("vps_data.json not found or corrupted, initializing empty data")
        return {}

def load_admin_data():
    try:
        with open('admin_data.json', 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        logger.warning("admin_data.json not found or corrupted, initializing with main admin")
        return {"admins": [str(MAIN_ADMIN_ID)]}

user_data = load_data()
vps_data = load_vps_data()
admin_data = load_admin_data()

def save_data():
    try:
        with open('user_data.json', 'w') as f:
            json.dump(user_data, f, indent=4)
        with open('vps_data.json', 'w') as f:
            json.dump(vps_data, f, indent=4)
        with open('admin_data.json', 'w') as f:
            json.dump(admin_data, f, indent=4)
        logger.info("Data saved successfully")
    except Exception as e:
        logger.error(f"Error saving data: {e}")

# ─── Helpers ───────────────────────────────────────────────────────────────────

def get_next_ssh_port():
    """Get the next available SSH port for a new container."""
    used_ports = set()
    for vps_list in vps_data.values():
        for vps in vps_list:
            if "ssh_port" in vps:
                used_ports.add(vps["ssh_port"])
    port = SSH_PORT_START
    while port in used_ports:
        port += 1
    return port

def generate_password(length=16):
    """Generate a random strong password."""
    chars = string.ascii_letters + string.digits + "!@#$%"
    return ''.join(random.choice(chars) for _ in range(length))

# ─── Admin checks ──────────────────────────────────────────────────────────────

def is_admin():
    async def predicate(ctx):
        user_id = str(ctx.author.id)
        if user_id == str(MAIN_ADMIN_ID) or user_id in admin_data.get("admins", []):
            return True
        await ctx.send(embed=create_error_embed("Access Denied", "You don't have permission to use this command."))
        return False
    return commands.check(predicate)

def is_main_admin():
    async def predicate(ctx):
        if str(ctx.author.id) == str(MAIN_ADMIN_ID):
            return True
        await ctx.send(embed=create_error_embed("Access Denied", "Only the main admin can use this command."))
        return False
    return commands.check(predicate)

# ─── Embed helpers ─────────────────────────────────────────────────────────────

def create_embed(title, description="", color=0x1a1a1a, fields=None):
    embed = discord.Embed(title=f"▌ {title}", description=description, color=color)
    embed.set_thumbnail(url="")
    if fields:
        for field in fields:
            embed.add_field(name=f"▸ {field['name']}", value=field["value"], inline=field.get("inline", False))
    embed.set_footer(
        text=f"Slick | VPS Manager • {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        icon_url=""
    )
    return embed

def create_success_embed(title, description=""):
    return create_embed(title, description, color=0x00ff88)

def create_error_embed(title, description=""):
    return create_embed(title, description, color=0xff3366)

def create_info_embed(title, description=""):
    return create_embed(title, description, color=0x00ccff)

def create_warning_embed(title, description=""):
    return create_embed(title, description, color=0xffaa00)

# ─── Docker execution ──────────────────────────────────────────────────────────

async def execute_docker(command, timeout=120):
    """Execute a Docker command with timeout and error handling."""
    try:
        cmd = shlex.split(command)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        if proc.returncode != 0:
            error = stderr.decode().strip() if stderr else "Command failed with no error output"
            raise Exception(error)
        return stdout.decode().strip() if stdout else True
    except asyncio.TimeoutError:
        logger.error(f"Docker command timed out: {command}")
        raise Exception(f"Command timed out after {timeout} seconds")
    except Exception as e:
        logger.error(f"Docker Error: {command} - {str(e)}")
        raise

async def docker_exec(container_name, command, timeout=60):
    """Execute a command inside a running Docker container."""
    cmd = ["docker", "exec", container_name, "bash", "-c", command]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    return stdout.decode().strip(), stderr.decode().strip(), proc.returncode

async def create_docker_container(container_name, ram_mb, cpu_count, ssh_port, password, disk_gb=30):
    """
    Create and configure a Docker container as a VPS.
    - Uses tmate for SSH access (no IPv4 required).
    - Installs OpenSSH server, tmate, and sets root password.
    """
    # Pull image if needed (silent)
    try:
        await execute_docker(f"docker pull {DOCKER_IMAGE}", timeout=300)
    except Exception:
        pass  # Image might already exist

    # Run container with resource limits (no port mapping needed for tmate)
    run_cmd = (
        f"docker run -d "
        f"--name {container_name} "
        f"--memory={ram_mb}m "
        f"--cpus={cpu_count} "
        f"--restart=unless-stopped "
        f"{DOCKER_IMAGE} "
        f"sleep infinity"
    )
    await execute_docker(run_cmd, timeout=60)

    # Install openssh-server, tmate, and configure SSH + root password
    setup_script = (
        "apt-get update -qq && "
        "apt-get install -y openssh-server tmate curl -qq && "
        "mkdir -p /var/run/sshd && "
        "echo 'PermitRootLogin yes' >> /etc/ssh/sshd_config && "
        "echo 'PasswordAuthentication yes' >> /etc/ssh/sshd_config && "
        f"echo 'root:{password}' | chpasswd && "
        "/usr/sbin/sshd"
    )
    stdout, stderr, rc = await docker_exec(container_name, setup_script, timeout=180)
    if rc != 0 and "already" not in stderr.lower():
        raise Exception(f"SSH setup failed: {stderr}")

    return True


async def get_tmate_session(container_name):
    """Start tmate inside container and return the SSH command string."""
    # Start tmate in background and get the SSH line
    tmate_script = (
        "pkill tmate 2>/dev/null || true && "
        "sleep 1 && "
        "tmate -S /tmp/tmate.sock new-session -d && "
        "sleep 3 && "
        "tmate -S /tmp/tmate.sock wait tmate-ready && "
        "tmate -S /tmp/tmate.sock display -p '#{tmate_ssh}'"
    )
    stdout, stderr, rc = await docker_exec(container_name, tmate_script, timeout=30)
    if rc != 0 or not stdout.strip():
        raise Exception(f"tmate session start failed: {stderr}")
    return stdout.strip()

# ─── VPS Role helper ───────────────────────────────────────────────────────────

async def get_or_create_vps_role(guild):
    global VPS_USER_ROLE_ID
    if VPS_USER_ROLE_ID:
        role = guild.get_role(VPS_USER_ROLE_ID)
        if role:
            return role
    role = discord.utils.get(guild.roles, name="VPS User")
    if role:
        VPS_USER_ROLE_ID = role.id
        return role
    try:
        role = await guild.create_role(
            name="VPS User",
            color=discord.Color.dark_purple(),
            reason="VPS User role for bot management",
            permissions=discord.Permissions.none()
        )
        VPS_USER_ROLE_ID = role.id
        logger.info(f"Created VPS User role: {role.name} (ID: {role.id})")
        return role
    except Exception as e:
        logger.error(f"Failed to create VPS User role: {e}")
        return None

# ─── CPU Monitor ───────────────────────────────────────────────────────────────

def get_cpu_usage():
    try:
        result = subprocess.run(['top', '-bn1'], capture_output=True, text=True)
        for line in result.stdout.split('\n'):
            if '%Cpu(s):' in line:
                for part in line.split(','):
                    if 'id,' in part:
                        idle = float(part.split('%')[0].split()[-1])
                        return 100.0 - idle
        return 0.0
    except Exception as e:
        logger.error(f"Error getting CPU usage: {e}")
        return 0.0

def cpu_monitor():
    global cpu_monitor_active
    while cpu_monitor_active:
        try:
            cpu_usage = get_cpu_usage()
            logger.info(f"Current CPU usage: {cpu_usage}%")
            if cpu_usage > CPU_THRESHOLD:
                logger.warning(f"CPU usage ({cpu_usage}%) exceeded threshold. Stopping all containers.")
                try:
                    subprocess.run(['docker', 'stop', '--time=5'] +
                                   [vps['container_name']
                                    for vps_list in vps_data.values()
                                    for vps in vps_list
                                    if vps.get('status') == 'running'],
                                   check=False)
                    for vps_list in vps_data.values():
                        for vps in vps_list:
                            if vps.get('status') == 'running':
                                vps['status'] = 'stopped'
                    save_data()
                except Exception as e:
                    logger.error(f"Error stopping containers: {e}")
            time.sleep(CHECK_INTERVAL)
        except Exception as e:
            logger.error(f"Error in CPU monitor: {e}")
            time.sleep(CHECK_INTERVAL)

cpu_thread = threading.Thread(target=cpu_monitor, daemon=True)
cpu_thread.start()

# ─── Bot events ────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    logger.info(f'{bot.user} has connected to Discord!')
    await bot.change_presence(activity=discord.Activity(
        type=discord.ActivityType.watching, name="Slick | VPS Manager"))
    if not auto_expire_check.is_running():
        auto_expire_check.start()
    logger.info("Bot is ready!")

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(embed=create_error_embed("Missing Argument", "Please use `!help` for command usage."))
    elif isinstance(error, commands.BadArgument):
        await ctx.send(embed=create_error_embed("Invalid Argument", "Please check your input and try again."))
    elif isinstance(error, commands.CheckFailure):
        pass
    else:
        logger.error(f"Command error: {error}")
        await ctx.send(embed=create_error_embed("System Error", "An error occurred. Please try again."))

# ─── ManageView ────────────────────────────────────────────────────────────────

class ManageView(discord.ui.View):
    def __init__(self, user_id, vps_list, is_shared=False, owner_id=None, is_admin=False):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.vps_list = vps_list
        self.selected_index = None
        self.is_shared = is_shared
        self.owner_id = owner_id or user_id
        self.is_admin = is_admin

        if len(vps_list) > 1:
            options = [
                discord.SelectOption(
                    label=f"VPS {i+1} ({v.get('plan', 'Custom')})",
                    description=f"Status: {v.get('status', 'unknown')}",
                    value=str(i)
                ) for i, v in enumerate(vps_list)
            ]
            self.select = discord.ui.Select(placeholder="Select a VPS to manage", options=options)
            self.select.callback = self.select_vps
            self.add_item(self.select)
            self.initial_embed = create_embed("VPS Management", "Select a VPS from the dropdown menu below.", 0x1a1a1a)
            self.initial_embed.add_field(
                name="Available VPS",
                value="\n".join([f"**VPS {i+1}:** `{v['container_name']}` - Status: `{v.get('status','unknown').upper()}`"
                                 for i, v in enumerate(vps_list)]),
                inline=False
            )
        else:
            self.selected_index = 0
            self.initial_embed = self.create_vps_embed(0)
            self.add_action_buttons()

    def create_vps_embed(self, index):
        vps = self.vps_list[index]
        status_color = 0x00ff88 if vps.get('status') == 'running' else 0xff3366
        owner_text = ""
        if self.is_admin and self.owner_id != self.user_id:
            try:
                owner_user = bot.get_user(int(self.owner_id))
                owner_text = f"\n**Owner:** {owner_user.mention}"
            except:
                owner_text = f"\n**Owner ID:** {self.owner_id}"
        embed = create_embed(
            f"VPS Management - VPS {index + 1}",
            f"Managing container: `{vps['container_name']}`{owner_text}",
            status_color
        )
        # Expire check
        expires = vps.get('expires')
        if expires and expires != "Never":
            try:
                exp_dt = datetime.fromisoformat(expires)
                days_left = (exp_dt - datetime.utcnow()).days
                expire_str = f"{expires[:10]} ({days_left}d left)" if days_left >= 0 else f"{expires[:10]} (**EXPIRED**)"
            except:
                expire_str = expires
        else:
            expire_str = "Never"

        resource_info = (
            f"**Plan:** {vps.get('plan', 'Custom')}\n"
            f"**Status:** `{vps.get('status', 'unknown').upper()}`\n"
            f"**RAM:** {vps['ram']}\n"
            f"**CPU:** {vps['cpu']} Core(s)\n"
            f"**Storage:** {vps.get('storage', '30GB')}\n"
            f"**SSH Port:** {vps.get('ssh_port', 'tmate')}\n"
            f"**Created:** {vps.get('created_at', '?')[:10]}\n"
            f"**Expires:** {expire_str}"
        )
        if "processor" in vps:
            resource_info += f"\n**Processor:** {vps['processor']}"
        embed.add_field(name="📊 Resources", value=resource_info, inline=False)
        embed.add_field(name="🎮 Controls",  value="Use the buttons below to manage your VPS", inline=False)
        return embed

    def add_action_buttons(self):
        if not self.is_shared and not self.is_admin:
            reinstall_button = discord.ui.Button(label="🔄 Reinstall", style=discord.ButtonStyle.danger)
            reinstall_button.callback = lambda inter: self.action_callback(inter, 'reinstall')
            self.add_item(reinstall_button)

        start_button = discord.ui.Button(label="▶ Start", style=discord.ButtonStyle.success)
        start_button.callback = lambda inter: self.action_callback(inter, 'start')

        stop_button = discord.ui.Button(label="⏸ Stop", style=discord.ButtonStyle.secondary)
        stop_button.callback = lambda inter: self.action_callback(inter, 'stop')

        ssh_button = discord.ui.Button(label="🔑 SSH", style=discord.ButtonStyle.primary)
        ssh_button.callback = lambda inter: self.action_callback(inter, 'ssh')

        self.add_item(start_button)
        self.add_item(stop_button)
        self.add_item(ssh_button)

    async def select_vps(self, interaction: discord.Interaction):
        if str(interaction.user.id) != self.user_id and not self.is_admin:
            await interaction.response.send_message(
                embed=create_error_embed("Access Denied", "This is not your VPS!"), ephemeral=True)
            return
        self.selected_index = int(self.select.values[0])
        new_embed = self.create_vps_embed(self.selected_index)
        self.clear_items()
        self.add_action_buttons()
        await interaction.response.edit_message(embed=new_embed, view=self)

    async def action_callback(self, interaction: discord.Interaction, action: str):
        if str(interaction.user.id) != self.user_id and not self.is_admin:
            await interaction.response.send_message(
                embed=create_error_embed("Access Denied", "This is not your VPS!"), ephemeral=True)
            return

        if self.is_shared:
            vps = vps_data[self.owner_id][self.selected_index]
        else:
            vps = self.vps_list[self.selected_index]

        container_name = vps["container_name"]

        if action == 'reinstall':
            if self.is_shared or self.is_admin:
                await interaction.response.send_message(
                    embed=create_error_embed("Access Denied", "Only the VPS owner can reinstall!"), ephemeral=True)
                return

            confirm_embed = create_warning_embed(
                "Reinstall Warning",
                f"⚠️ **WARNING:** This will erase all data on VPS `{container_name}` and reinstall Ubuntu 22.04.\n\n"
                f"This action cannot be undone. Continue?"
            )

            class ConfirmView(discord.ui.View):
                def __init__(self, parent_view, container_name, vps, owner_id, selected_index):
                    super().__init__(timeout=60)
                    self.parent_view = parent_view
                    self.container_name = container_name
                    self.vps = vps
                    self.owner_id = owner_id
                    self.selected_index = selected_index

                @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
                async def confirm(self, interaction: discord.Interaction, item: discord.ui.Button):
                    await interaction.response.defer(ephemeral=True)
                    try:
                        await interaction.followup.send(
                            embed=create_info_embed("Deleting Container", f"Removing `{self.container_name}`..."),
                            ephemeral=True)
                        # Stop and remove old container
                        try:
                            await execute_docker(f"docker stop {self.container_name}")
                        except:
                            pass
                        await execute_docker(f"docker rm -f {self.container_name}")

                        # Recreate
                        await interaction.followup.send(
                            embed=create_info_embed("Recreating Container", f"Creating new container `{self.container_name}`..."),
                            ephemeral=True)
                        original_ram = self.vps["ram"]
                        original_cpu = self.vps["cpu"]
                        original_disk = int(self.vps.get("storage", "30GB").replace("GB", ""))
                        ram_mb = int(original_ram.replace("GB", "")) * 1024
                        new_password = generate_password()
                        await create_docker_container(self.container_name, ram_mb, original_cpu, 0, new_password, disk_gb=original_disk)
                        self.vps["status"] = "running"
                        self.vps["ssh_password"] = new_password
                        self.vps["created_at"] = datetime.now().isoformat()
                        save_data()
                        await interaction.followup.send(
                            embed=create_success_embed("Reinstall Complete",
                                                       f"VPS `{self.container_name}` reinstalled successfully!"),
                            ephemeral=True)
                        if not self.parent_view.is_shared:
                            await interaction.message.edit(
                                embed=self.parent_view.create_vps_embed(self.parent_view.selected_index),
                                view=self.parent_view)
                    except Exception as e:
                        await interaction.followup.send(
                            embed=create_error_embed("Reinstall Failed", f"Error: {str(e)}"), ephemeral=True)

                @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
                async def cancel(self, interaction: discord.Interaction, item: discord.ui.Button):
                    await interaction.response.edit_message(
                        embed=self.parent_view.create_vps_embed(self.parent_view.selected_index),
                        view=self.parent_view)

            await interaction.response.send_message(
                embed=confirm_embed,
                view=ConfirmView(self, container_name, vps, self.owner_id, self.selected_index),
                ephemeral=True)

        elif action == 'start':
            await interaction.response.defer(ephemeral=True)
            try:
                await execute_docker(f"docker start {container_name}")
                # Restart SSH inside container
                await docker_exec(container_name, "/usr/sbin/sshd || true", timeout=10)
                vps["status"] = "running"
                save_data()
                await interaction.followup.send(
                    embed=create_success_embed("VPS Started", f"VPS `{container_name}` is now running!"),
                    ephemeral=True)
                await interaction.message.edit(embed=self.create_vps_embed(self.selected_index), view=self)
            except Exception as e:
                await interaction.followup.send(
                    embed=create_error_embed("Start Failed", str(e)), ephemeral=True)

        elif action == 'stop':
            await interaction.response.defer(ephemeral=True)
            try:
                await execute_docker(f"docker stop {container_name}", timeout=120)
                vps["status"] = "stopped"
                save_data()
                await interaction.followup.send(
                    embed=create_success_embed("VPS Stopped", f"VPS `{container_name}` has been stopped!"),
                    ephemeral=True)
                await interaction.message.edit(embed=self.create_vps_embed(self.selected_index), view=self)
            except Exception as e:
                await interaction.followup.send(
                    embed=create_error_embed("Stop Failed", str(e)), ephemeral=True)

        elif action == 'ssh':
            await interaction.response.defer(ephemeral=True)
            try:
                ssh_password = vps.get("ssh_password")

                if not ssh_password:
                    await interaction.followup.send(
                        embed=create_error_embed("SSH Error", "SSH credentials not found. Please reinstall the VPS."),
                        ephemeral=True)
                    return

                await interaction.followup.send(
                    embed=create_info_embed("Starting tmate", "Generating tmate SSH session, please wait..."),
                    ephemeral=True)

                tmate_cmd = await get_tmate_session(container_name)

                ssh_embed = create_embed("🔑 SSH Access", f"SSH connection for VPS `{container_name}`:", 0x00ff88)
                ssh_embed.add_field(
                    name="SSH Command (tmate)",
                    value=f"```{tmate_cmd}```",
                    inline=False
                )
                ssh_embed.add_field(name="Password", value=f"```{ssh_password}```", inline=True)
                ssh_embed.add_field(
                    name="⚠️ Note",
                    value="• tmate session expires when VPS restarts\n• Click SSH again after restart to get new session\n• Change your password after first login!",
                    inline=False
                )

                try:
                    await interaction.user.send(embed=ssh_embed)
                    await interaction.followup.send(
                        embed=create_success_embed("SSH Sent", "Check your DMs for tmate SSH credentials!"),
                        ephemeral=True)
                except discord.Forbidden:
                    await interaction.followup.send(
                        embed=create_error_embed("DM Failed", "Enable DMs to receive SSH credentials!"),
                        ephemeral=True)
            except Exception as e:
                await interaction.followup.send(
                    embed=create_error_embed("SSH Error", str(e)), ephemeral=True)


# ─── Commands ──────────────────────────────────────────────────────────────────

@bot.command(name='create')
@is_admin()
async def create_vps(ctx, user: discord.Member, ram: int, cpu: int, disk: int = 30):
    """Create a custom VPS for a user (Admin only) — !create @user <ram_GB> <cpu_cores> <disk_GB>"""
    if ram <= 0 or cpu <= 0 or disk <= 0:
        await ctx.send(embed=create_error_embed("Invalid Specs", "RAM, CPU and Disk must be positive integers.\nUsage: `!create @user <ram_GB> <cpu_cores> <disk_GB>`"))
        return

    user_id = str(user.id)
    if user_id not in vps_data:
        vps_data[user_id] = []

    vps_count = len(vps_data[user_id]) + 1
    container_name = f"vps-{user_id}-{vps_count}"
    ram_mb = ram * 1024
    password = generate_password()

    await ctx.send(embed=create_info_embed("Creating VPS", f"Deploying Docker VPS for {user.mention}...\n🧠 RAM: `{ram}GB` | ⚙️ CPU: `{cpu} Core(s)` | 💾 Disk: `{disk}GB`"))

    try:
        await create_docker_container(container_name, ram_mb, cpu, 0, password, disk_gb=disk)

        vps_info = {
            "container_name": container_name,
            "ram": f"{ram}GB",
            "cpu": str(cpu),
            "storage": f"{disk}GB",
            "status": "running",
            "created_at": datetime.now().isoformat(),
            "expires": "Never",
            "ssh_password": password,
            "shared_with": []
        }
        vps_data[user_id].append(vps_info)
        save_data()

        if ctx.guild:
            vps_role = await get_or_create_vps_role(ctx.guild)
            if vps_role:
                try:
                    await user.add_roles(vps_role, reason="VPS ownership granted")
                except discord.Forbidden:
                    pass

        embed = create_embed("🚀 VPS Deployed!", f"{user.mention} your VPS is live! Check your **DMs** for SSH access.", color=0x00ff88)
        embed.add_field(name="👤 Owner",     value=user.mention,          inline=True)
        embed.add_field(name="🆔 VPS ID",    value=f"`#{vps_count}`",     inline=True)
        embed.add_field(name="📦 Container", value=f"`{container_name}`", inline=True)
        embed.add_field(name="🧠 RAM",       value=f"`{ram} GB`",         inline=True)
        embed.add_field(name="⚙️ CPU",       value=f"`{cpu} Core(s)`",    inline=True)
        embed.add_field(name="💾 Disk",      value=f"`{disk} GB`",        inline=True)
        embed.add_field(name="🎮 Manage",    value="Use `!manage` → **Start / Stop / Reinstall / SSH**", inline=False)
        await ctx.send(embed=embed)

        # DM user
        try:
            tmate_cmd = await get_tmate_session(container_name)

            dm_embed = create_embed("🎉 Your VPS is Ready!", "Connect now using the command below.", color=0x5865F2)
            dm_embed.add_field(name="🆔 VPS ID",  value=f"`#{vps_count}`",    inline=True)
            dm_embed.add_field(name="🧠 RAM",      value=f"`{ram} GB`",        inline=True)
            dm_embed.add_field(name="⚙️ CPU",      value=f"`{cpu} Core(s)`",   inline=True)
            dm_embed.add_field(
                name="🔗 SSH Command",
                value=f"```{tmate_cmd}```",
                inline=False
            )
            dm_embed.add_field(
                name="📌 How to Connect",
                value="1️⃣ Copy the command above\n2️⃣ Paste in CMD / Termux / PuTTY\n3️⃣ You\'re in! 🚀",
                inline=False
            )
            dm_embed.add_field(
                name="🎮 Manage Your VPS",
                value="Use `!manage` in the server to **Start / Stop / Reinstall / SSH**",
                inline=False
            )
            await user.send(embed=dm_embed)
        except discord.Forbidden:
            pass

    except Exception as e:
        await ctx.send(embed=create_error_embed("Creation Failed", f"Error: {str(e)}"))


@bot.command(name='manage')
async def manage_vps(ctx, user: discord.Member = None):
    """Manage your VPS or another user's VPS (Admin only)"""
    if user:
        if not (str(ctx.author.id) == str(MAIN_ADMIN_ID) or str(ctx.author.id) in admin_data.get("admins", [])):
            await ctx.send(embed=create_error_embed("Access Denied", "Only admins can manage other users' VPS."))
            return
        user_id = str(user.id)
        vps_list = vps_data.get(user_id, [])
        if not vps_list:
            await ctx.send(embed=create_error_embed("No VPS Found", f"{user.mention} doesn't have any VPS."))
            return
        view = ManageView(str(ctx.author.id), vps_list, is_admin=True, owner_id=user_id)
        await ctx.send(embed=create_info_embed(f"Managing {user.name}'s VPS", f"Managing VPS for {user.mention}"), view=view)
    else:
        user_id = str(ctx.author.id)
        vps_list = vps_data.get(user_id, [])
        if not vps_list:
            embed = create_embed("No VPS Found", "You don't have any VPS. Use `.buywc` to purchase one.", 0xff3366)
            embed.add_field(name="Quick Actions", value="• `!plans` - View plans\n• `!buywc <plan> <processor>` - Purchase VPS", inline=False)
            await ctx.send(embed=embed)
            return
        view = ManageView(user_id, vps_list)
        await ctx.send(embed=view.initial_embed, view=view)


@bot.command(name='delete-vps')
@is_admin()
async def delete_vps(ctx, user: discord.Member, vps_number: int, *, reason: str = "No reason"):
    """Delete a user's VPS (Admin only)"""
    user_id = str(user.id)
    if user_id not in vps_data or vps_number < 1 or vps_number > len(vps_data[user_id]):
        await ctx.send(embed=create_error_embed("Invalid VPS", "Invalid VPS number or user doesn't have a VPS."))
        return

    vps = vps_data[user_id][vps_number - 1]
    container_name = vps["container_name"]
    await ctx.send(embed=create_info_embed("Deleting VPS", f"Removing VPS #{vps_number}..."))

    try:
        try:
            await execute_docker(f"docker stop {container_name}")
        except:
            pass
        await execute_docker(f"docker rm -f {container_name}")
        del vps_data[user_id][vps_number - 1]
        if not vps_data[user_id]:
            del vps_data[user_id]
            if ctx.guild:
                vps_role = await get_or_create_vps_role(ctx.guild)
                if vps_role and vps_role in user.roles:
                    try:
                        await user.remove_roles(vps_role, reason="No VPS ownership")
                    except discord.Forbidden:
                        pass
        save_data()
        embed = create_success_embed("VPS Deleted Successfully")
        embed.add_field(name="Owner", value=user.mention, inline=True)
        embed.add_field(name="VPS ID", value=f"#{vps_number}", inline=True)
        embed.add_field(name="Container", value=f"`{container_name}`", inline=True)
        embed.add_field(name="Reason", value=reason, inline=False)
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(embed=create_error_embed("Deletion Failed", f"Error: {str(e)}"))


@bot.command(name='list-all')
@is_admin()
async def list_all_vps(ctx):
    """List all VPS and user information (Admin only)"""
    embed = create_embed("All VPS Information", "Complete overview of all VPS deployments", 0x1a1a1a)
    total_vps = 0
    running_vps = 0
    stopped_vps = 0
    vps_info = []
    user_summary = []

    for user_id, vps_list in vps_data.items():
        try:
            user = await bot.fetch_user(int(user_id))
            user_vps_count = len(vps_list)
            user_running = sum(1 for vps in vps_list if vps.get('status') == 'running')
            total_vps += user_vps_count
            running_vps += user_running
            stopped_vps += user_vps_count - user_running
            user_summary.append(f"**{user.name}** ({user.mention}) - {user_vps_count} VPS ({user_running} running)")
            for i, vps in enumerate(vps_list):
                status_emoji = "🟢" if vps.get('status') == 'running' else "🔴"
                vps_info.append(
                    f"{status_emoji} **{user.name}** - VPS {i+1}: `{vps['container_name']}` "
                    f"Port:{vps.get('ssh_port','?')} - {vps.get('status','unknown').upper()}"
                )
        except discord.NotFound:
            vps_info.append(f"❓ Unknown User ({user_id}) - {len(vps_list)} VPS")

    embed.add_field(
        name="System Overview",
        value=f"**Total Users:** {len(vps_data)}\n**Total VPS:** {total_vps}\n**Running:** {running_vps}\n**Stopped:** {stopped_vps}",
        inline=False
    )
    if user_summary:
        embed.add_field(name="User Summary", value="\n".join(user_summary[:10]), inline=False)
    if vps_info:
        for i in range(0, min(len(vps_info), 30), 15):
            chunk = vps_info[i:i+15]
            embed.add_field(name=f"VPS Deployments ({i+1}-{min(i+15, len(vps_info))})", value="\n".join(chunk), inline=False)
    await ctx.send(embed=embed)


@bot.command(name='manage-shared')
async def manage_shared_vps(ctx, owner: discord.Member, vps_number: int):
    """Manage a shared VPS"""
    owner_id = str(owner.id)
    user_id = str(ctx.author.id)
    if owner_id not in vps_data or vps_number < 1 or vps_number > len(vps_data[owner_id]):
        await ctx.send(embed=create_error_embed("Invalid VPS", "Invalid VPS number."))
        return
    vps = vps_data[owner_id][vps_number - 1]
    if user_id not in vps.get("shared_with", []):
        await ctx.send(embed=create_error_embed("Access Denied", "You do not have access to this VPS."))
        return
    view = ManageView(user_id, [vps], is_shared=True, owner_id=owner_id)
    await ctx.send(embed=view.initial_embed, view=view)


@bot.command(name='share-user')
async def share_user(ctx, shared_user: discord.Member, vps_number: int):
    """Share VPS access with another user"""
    user_id = str(ctx.author.id)
    shared_user_id = str(shared_user.id)
    if user_id not in vps_data or vps_number < 1 or vps_number > len(vps_data[user_id]):
        await ctx.send(embed=create_error_embed("Invalid VPS", "Invalid VPS number."))
        return
    vps = vps_data[user_id][vps_number - 1]
    if "shared_with" not in vps:
        vps["shared_with"] = []
    if shared_user_id in vps["shared_with"]:
        await ctx.send(embed=create_error_embed("Already Shared", f"{shared_user.mention} already has access!"))
        return
    vps["shared_with"].append(shared_user_id)
    save_data()
    await ctx.send(embed=create_success_embed("VPS Shared", f"VPS #{vps_number} shared with {shared_user.mention}!"))
    try:
        await shared_user.send(embed=create_embed(
            "VPS Access Granted",
            f"You have access to VPS #{vps_number} from {ctx.author.mention}. Use `!manage-shared {ctx.author.mention} {vps_number}`",
            0x00ff88
        ))
    except discord.Forbidden:
        pass


@bot.command(name='share-ruser')
async def revoke_share(ctx, shared_user: discord.Member, vps_number: int):
    """Revoke shared VPS access"""
    user_id = str(ctx.author.id)
    shared_user_id = str(shared_user.id)
    if user_id not in vps_data or vps_number < 1 or vps_number > len(vps_data[user_id]):
        await ctx.send(embed=create_error_embed("Invalid VPS", "Invalid VPS number."))
        return
    vps = vps_data[user_id][vps_number - 1]
    if "shared_with" not in vps or shared_user_id not in vps["shared_with"]:
        await ctx.send(embed=create_error_embed("Not Shared", f"{shared_user.mention} doesn't have access!"))
        return
    vps["shared_with"].remove(shared_user_id)
    save_data()
    await ctx.send(embed=create_success_embed("Access Revoked", f"Access to VPS #{vps_number} revoked from {shared_user.mention}!"))


@bot.command(name='buywc')
async def buy_with_credits(ctx, plan: str, processor: str = "Intel"):
    """Buy a VPS with credits"""
    user_id = str(ctx.author.id)
    prices = {
        "Starter": {"Intel": 42, "AMD": 83},
        "Basic": {"Intel": 96, "AMD": 164},
        "Standard": {"Intel": 192, "AMD": 320},
        "Pro": {"Intel": 220, "AMD": 340}
    }
    plans = {
        "Starter": {"ram": "4GB", "cpu": "1", "storage": "10GB"},
        "Basic": {"ram": "8GB", "cpu": "1", "storage": "10GB"},
        "Standard": {"ram": "12GB", "cpu": "2", "storage": "10GB"},
        "Pro": {"ram": "16GB", "cpu": "2", "storage": "10GB"}
    }
    if plan not in prices:
        await ctx.send(embed=create_error_embed("Invalid Plan", "Available: Starter, Basic, Standard, Pro"))
        return
    if processor not in ["Intel", "AMD"]:
        await ctx.send(embed=create_error_embed("Invalid Processor", "Choose: Intel or AMD"))
        return

    cost = prices[plan][processor]
    if user_id not in user_data:
        user_data[user_id] = {"credits": 0}
    if user_data[user_id]["credits"] < cost:
        await ctx.send(embed=create_error_embed("Insufficient Credits",
                                                f"You need {cost} credits but have {user_data[user_id]['credits']}"))
        return

    user_data[user_id]["credits"] -= cost
    if user_id not in vps_data:
        vps_data[user_id] = []

    vps_count = len(vps_data[user_id]) + 1
    container_name = f"vps-{user_id}-{vps_count}"
    ram_str = plans[plan]["ram"]
    cpu_str = plans[plan]["cpu"]
    ram_mb = int(ram_str.replace("GB", "")) * 1024
    password = generate_password()

    await ctx.send(embed=create_info_embed("Processing Purchase", f"Deploying {plan} Docker VPS..."))

    try:
        await create_docker_container(container_name, ram_mb, cpu_str, 0, password)

        vps_info = {
            "plan": plan,
            "container_name": container_name,
            "ram": ram_str,
            "cpu": cpu_str,
            "storage": plans[plan]["storage"],
            "status": "running",
            "created_at": datetime.now().isoformat(),
            "processor": processor,
            "ssh_password": password,
            "shared_with": []
        }
        vps_data[user_id].append(vps_info)
        save_data()

        if ctx.guild:
            vps_role = await get_or_create_vps_role(ctx.guild)
            if vps_role:
                try:
                    await ctx.author.add_roles(vps_role, reason="VPS purchase completed")
                except discord.Forbidden:
                    pass

        embed = create_success_embed("VPS Purchased Successfully")
        embed.add_field(name="Plan", value=f"**{plan}** ({processor})", inline=True)
        embed.add_field(name="VPS ID", value=f"#{vps_count}", inline=True)
        embed.add_field(name="Container", value=f"`{container_name}`", inline=True)
        embed.add_field(name="Cost", value=f"{cost} credits", inline=True)
        embed.add_field(name="Resources",
                        value=f"**RAM:** {ram_str}\n**CPU:** {cpu_str} Cores\n**Storage:** 10GB", inline=False)
        await ctx.send(embed=embed)

        try:
            tmate_cmd = await get_tmate_session(container_name)

            dm_embed = create_embed("🎉 Your VPS is Ready!", "Connect now using the command below.", color=0x5865F2)
            dm_embed.add_field(name="🆔 VPS ID",  value=f"`#{vps_count}`",      inline=True)
            dm_embed.add_field(name="🧠 RAM",      value=f"`{ram_str}`",          inline=True)
            dm_embed.add_field(name="⚙️ CPU",      value=f"`{cpu_str} Core(s)`",  inline=True)
            dm_embed.add_field(
                name="🔗 SSH Command",
                value=f"```{tmate_cmd}```",
                inline=False
            )
            dm_embed.add_field(
                name="📌 How to Connect",
                value="1️⃣ Copy the command above\n2️⃣ Paste in CMD / Termux / PuTTY\n3️⃣ You\'re in! 🚀",
                inline=False
            )
            dm_embed.add_field(
                name="🎮 Manage Your VPS",
                value="Use `!manage` in the server to **Start / Stop / Reinstall / SSH**",
                inline=False
            )
            await ctx.author.send(embed=dm_embed)
        except discord.Forbidden:
            pass

    except Exception as e:
        # Refund credits on failure
        user_data[user_id]["credits"] += cost
        save_data()
        await ctx.send(embed=create_error_embed("Purchase Failed", f"Error: {str(e)}\n\nCredits refunded."))


@bot.command(name='buyc')
async def buy_credits(ctx):
    """Get payment information"""
    embed = create_embed("💳 Purchase Credits", "Choose your payment method below:", 0x1a1a1a)
    embed.add_field(name="🇮🇳 UPI", value="```\n9526303242@fam\n```", inline=False)
    embed.add_field(name="💰 PayPal", value="```\nexample@paypal.com\n```", inline=False)
    embed.add_field(name="₿ Crypto", value="BTC, ETH, USDT accepted", inline=False)
    embed.add_field(name="📋 Next Steps", value="1. Pay\n2. Contact admin with transaction ID\n3. Receive credits", inline=False)
    try:
        await ctx.author.send(embed=embed)
        await ctx.send(embed=create_success_embed("Information Sent", "Payment details sent to your DMs!"))
    except discord.Forbidden:
        await ctx.send(embed=create_error_embed("DM Failed", "Enable DMs to receive payment info!"))


@bot.command(name='plans')
async def show_plans(ctx):
    """Show available VPS plans"""
    embed = create_embed("💎 VPS Plans - Slick", "Choose your perfect VPS plan:", 0x1a1a1a)
    plans_info = [
        ("🥉 Starter", "**RAM:** 4GB\n**CPU:** 1 Core\n**Storage:** 10GB\n**Intel:** 42 credits\n**AMD:** 83 credits"),
        ("🥈 Basic", "**RAM:** 8GB\n**CPU:** 1 Core\n**Storage:** 10GB\n**Intel:** 96 credits\n**AMD:** 164 credits"),
        ("🥇 Standard", "**RAM:** 12GB\n**CPU:** 2 Cores\n**Storage:** 10GB\n**Intel:** 192 credits\n**AMD:** 320 credits"),
        ("💎 Pro", "**RAM:** 16GB\n**CPU:** 2 Cores\n**Storage:** 10GB\n**Intel:** 220 credits\n**AMD:** 340 credits"),
    ]
    for name, value in plans_info:
        embed.add_field(name=name, value=value, inline=True)
    embed.add_field(name="How to Buy", value="Use `!buywc <plan> <Intel/AMD>` to purchase\nUse `!buyc` for payment info", inline=False)
    await ctx.send(embed=embed)


@bot.command(name='credits')
async def check_credits(ctx):
    """Check your credit balance"""
    user_id = str(ctx.author.id)
    if user_id not in user_data:
        user_data[user_id] = {"credits": 0}
        save_data()
    credits = user_data[user_id].get("credits", 0)
    embed = create_info_embed("💰 Credit Balance", f"{ctx.author.mention}, you have **{credits}** credits.")
    await ctx.send(embed=embed)


@bot.command(name='adminc')
@is_admin()
async def admin_add_credits(ctx, user: discord.Member, amount: int):
    """Add credits to a user (Admin only)"""
    user_id = str(user.id)
    if user_id not in user_data:
        user_data[user_id] = {"credits": 0}
    user_data[user_id]["credits"] += amount
    save_data()
    await ctx.send(embed=create_success_embed("Credits Added",
                                              f"Added **{amount}** credits to {user.mention}. "
                                              f"New balance: **{user_data[user_id]['credits']}**"))


@bot.command(name='adminrc')
@is_admin()
async def admin_remove_credits(ctx, user: discord.Member, amount: str):
    """Remove credits from a user (Admin only). Use 'all' to remove all."""
    user_id = str(user.id)
    if user_id not in user_data:
        user_data[user_id] = {"credits": 0}
    if amount.lower() == "all":
        removed = user_data[user_id]["credits"]
        user_data[user_id]["credits"] = 0
    else:
        removed = int(amount)
        user_data[user_id]["credits"] = max(0, user_data[user_id]["credits"] - removed)
    save_data()
    await ctx.send(embed=create_success_embed("Credits Removed",
                                              f"Removed **{removed}** credits from {user.mention}. "
                                              f"New balance: **{user_data[user_id]['credits']}**"))


@bot.command(name='userinfo')
@is_admin()
async def user_info(ctx, user: discord.Member):
    """Get detailed user information (Admin only)"""
    user_id = str(user.id)
    credits = user_data.get(user_id, {}).get("credits", 0)
    embed = create_embed(f"👤 User Info - {user.name}", "", 0x1a1a1a)
    embed.add_field(name="User", value=f"{user.mention}\n**ID:** {user.id}", inline=False)
    embed.add_field(name="💰 Credits", value=f"**{credits}**", inline=True)
    vps_list = vps_data.get(user_id, [])
    if vps_list:
        vps_text = "\n".join([
            f"VPS {i+1}: `{v['container_name']}` | Port:{v.get('ssh_port','?')} | {v.get('status','?').upper()}"
            for i, v in enumerate(vps_list)
        ])
        embed.add_field(name="🖥️ VPS", value=vps_text, inline=False)
    else:
        embed.add_field(name="🖥️ VPS", value="No VPS owned", inline=False)
    is_admin_user = user_id == str(MAIN_ADMIN_ID) or user_id in admin_data.get("admins", [])
    embed.add_field(name="🛡️ Admin", value="Yes" if is_admin_user else "No", inline=True)
    await ctx.send(embed=embed)


@bot.command(name='serverstats')
@is_admin()
async def server_stats(ctx):
    """Show server statistics (Admin only)"""
    total_vps = sum(len(v) for v in vps_data.values())
    running_vps = sum(1 for vl in vps_data.values() for v in vl if v.get('status') == 'running')
    total_credits = sum(u.get('credits', 0) for u in user_data.values())
    total_ram = sum(int(v['ram'].replace('GB', '')) for vl in vps_data.values() for v in vl)
    total_cpu = sum(int(v['cpu']) for vl in vps_data.values() for v in vl)

    embed = create_embed("📊 Server Statistics", "Current server overview", 0x1a1a1a)
    embed.add_field(name="👥 Users", value=f"**Total:** {len(user_data)}\n**Admins:** {len(admin_data.get('admins', [])) + 1}", inline=False)
    embed.add_field(name="🖥️ VPS", value=f"**Total:** {total_vps}\n**Running:** {running_vps}\n**Stopped:** {total_vps - running_vps}", inline=False)
    embed.add_field(name="💰 Economy", value=f"**Total Credits:** {total_credits}", inline=False)
    embed.add_field(name="📈 Resources", value=f"**Total RAM:** {total_ram}GB\n**Total CPU:** {total_cpu} cores", inline=False)
    await ctx.send(embed=embed)


@bot.command(name='vpsinfo')
@is_admin()
async def vps_info(ctx, container_name: str = None):
    """Get VPS information (Admin only)"""
    if not container_name:
        all_vps = []
        for uid, vl in vps_data.items():
            try:
                u = await bot.fetch_user(int(uid))
                for i, v in enumerate(vl):
                    all_vps.append(f"**{u.name}** - VPS {i+1}: `{v['container_name']}` Port:{v.get('ssh_port','?')} - {v.get('status','?').upper()}")
            except:
                pass
        embed = create_embed("🖥️ All VPS", f"Total: {len(all_vps)}", 0x1a1a1a)
        for i in range(0, len(all_vps), 20):
            embed.add_field(name=f"VPS List ({i+1}-{i+20})", value="\n".join(all_vps[i:i+20]), inline=False)
        await ctx.send(embed=embed)
    else:
        found_vps = None
        found_user = None
        for uid, vl in vps_data.items():
            for v in vl:
                if v['container_name'] == container_name:
                    found_vps = v
                    found_user = await bot.fetch_user(int(uid))
                    break
            if found_vps:
                break
        if not found_vps:
            await ctx.send(embed=create_error_embed("Not Found", f"No VPS with name `{container_name}`"))
            return
        embed = create_embed(f"🖥️ VPS - {container_name}", f"Owned by {found_user.mention}", 0x1a1a1a)
        embed.add_field(name="Specs", value=f"**RAM:** {found_vps['ram']}\n**CPU:** {found_vps['cpu']} Cores", inline=True)
        embed.add_field(name="Status", value=f"**{found_vps.get('status','?').upper()}**", inline=True)
        embed.add_field(name="SSH Port", value=f"`{found_vps.get('ssh_port','N/A')}`", inline=True)
        embed.add_field(name="Created", value=found_vps.get('created_at', 'Unknown'), inline=False)
        await ctx.send(embed=embed)


@bot.command(name='restart-vps')
@is_admin()
async def restart_vps(ctx, container_name: str):
    """Restart a VPS (Admin only)"""
    await ctx.send(embed=create_info_embed("Restarting VPS", f"Restarting `{container_name}`..."))
    try:
        await execute_docker(f"docker restart {container_name}")
        # Restart SSH
        await asyncio.sleep(3)
        await docker_exec(container_name, "/usr/sbin/sshd || true", timeout=10)
        for vl in vps_data.values():
            for v in vl:
                if v['container_name'] == container_name:
                    v['status'] = 'running'
                    save_data()
                    break
        await ctx.send(embed=create_success_embed("VPS Restarted", f"`{container_name}` restarted successfully!"))
    except Exception as e:
        await ctx.send(embed=create_error_embed("Restart Failed", str(e)))


@bot.command(name='backup-vps')
@is_admin()
async def backup_vps(ctx, container_name: str):
    """Create a Docker image snapshot of a VPS (Admin only)"""
    snapshot_name = f"{container_name}-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    await ctx.send(embed=create_info_embed("Creating Backup", f"Committing snapshot of `{container_name}`..."))
    try:
        await execute_docker(f"docker commit {container_name} {snapshot_name}")
        await ctx.send(embed=create_success_embed("Backup Created", f"Image `{snapshot_name}` created!"))
    except Exception as e:
        await ctx.send(embed=create_error_embed("Backup Failed", str(e)))


@bot.command(name='restore-vps')
@is_admin()
async def restore_vps(ctx, container_name: str, snapshot_name: str):
    """Restore a VPS from a Docker snapshot image (Admin only)"""
    await ctx.send(embed=create_info_embed("Restoring VPS", f"Restoring `{container_name}` from `{snapshot_name}`..."))
    try:
        # Find VPS info for port/password
        found_vps = None
        for vl in vps_data.values():
            for v in vl:
                if v['container_name'] == container_name:
                    found_vps = v
                    break
        if not found_vps:
            await ctx.send(embed=create_error_embed("Not Found", f"No VPS data for `{container_name}`"))
            return

        # Stop and remove current container
        try:
            await execute_docker(f"docker stop {container_name}")
        except:
            pass
        await execute_docker(f"docker rm -f {container_name}")

        # Recreate from snapshot image
        ssh_port = found_vps.get("ssh_port", get_next_ssh_port())
        run_cmd = (
            f"docker run -d "
            f"--name {container_name} "
            f"-p {ssh_port}:22 "
            f"--restart=unless-stopped "
            f"{snapshot_name} "
            f"sleep infinity"
        )
        await execute_docker(run_cmd)
        await asyncio.sleep(2)
        await docker_exec(container_name, "/usr/sbin/sshd || true", timeout=10)
        found_vps["status"] = "running"
        save_data()
        await ctx.send(embed=create_success_embed("VPS Restored", f"`{container_name}` restored from `{snapshot_name}`!"))
    except Exception as e:
        await ctx.send(embed=create_error_embed("Restore Failed", str(e)))


@bot.command(name='list-snapshots')
@is_admin()
async def list_snapshots(ctx, container_name: str):
    """List Docker image snapshots for a VPS (Admin only)"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "images", "--format", "{{.Repository}}:{{.Tag}} ({{.Size}})",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        all_images = stdout.decode().strip().split('\n')
        snapshots = [img for img in all_images if img.startswith(container_name + "-backup-")]
        if snapshots:
            embed = create_embed(f"📸 Snapshots for {container_name}", f"Found {len(snapshots)} snapshots", 0x1a1a1a)
            embed.add_field(name="Snapshots", value="\n".join([f"• `{s}`" for s in snapshots]), inline=False)
        else:
            embed = create_info_embed("No Snapshots", f"No snapshots found for `{container_name}`")
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(embed=create_error_embed("Error", str(e)))


@bot.command(name='exec')
@is_admin()
async def execute_command(ctx, container_name: str, *, command: str):
    """Execute a command inside a VPS container (Admin only)"""
    await ctx.send(embed=create_info_embed("Executing Command", f"Running in `{container_name}`..."))
    try:
        stdout, stderr, rc = await docker_exec(container_name, command, timeout=30)
        embed = create_embed(f"Command Output - {container_name}", f"Command: `{command}`", 0x1a1a1a)
        if stdout:
            out = stdout[:1000] + "\n...(truncated)" if len(stdout) > 1000 else stdout
            embed.add_field(name="📤 Output", value=f"```\n{out}\n```", inline=False)
        if stderr:
            err = stderr[:1000] + "\n...(truncated)" if len(stderr) > 1000 else stderr
            embed.add_field(name="⚠️ Stderr", value=f"```\n{err}\n```", inline=False)
        embed.add_field(name="🔄 Exit Code", value=f"**{rc}**", inline=False)
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(embed=create_error_embed("Execution Failed", str(e)))


@bot.command(name='stop-vps-all')
@is_admin()
async def stop_all_vps(ctx):
    """Stop all VPS containers (Admin only)"""
    await ctx.send(embed=create_warning_embed("Stopping All VPS",
                                              "⚠️ This will stop ALL running VPS. Continue?"))

    class ConfirmView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=60)

        @discord.ui.button(label="Stop All VPS", style=discord.ButtonStyle.danger)
        async def confirm(self, interaction: discord.Interaction, item: discord.ui.Button):
            await interaction.response.defer()
            stopped_count = 0
            errors = []
            for vl in vps_data.values():
                for v in vl:
                    if v.get('status') == 'running':
                        try:
                            proc = await asyncio.create_subprocess_exec(
                                "docker", "stop", v['container_name'],
                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                            )
                            await proc.communicate()
                            v['status'] = 'stopped'
                            stopped_count += 1
                        except Exception as e:
                            errors.append(str(e))
            save_data()
            embed = create_success_embed("All VPS Stopped", f"Stopped **{stopped_count}** containers.")
            if errors:
                embed.add_field(name="Errors", value="\n".join(errors[:5]), inline=False)
            await interaction.followup.send(embed=embed)

        @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
        async def cancel(self, interaction: discord.Interaction, item: discord.ui.Button):
            await interaction.response.edit_message(embed=create_info_embed("Cancelled", "Operation cancelled."))

    await ctx.send(view=ConfirmView())


@bot.command(name='cpu-monitor')
@is_admin()
async def cpu_monitor_control(ctx, action: str = "status"):
    """Control CPU monitoring (Admin only)"""
    global cpu_monitor_active
    if action.lower() == "status":
        status = "Active" if cpu_monitor_active else "Inactive"
        embed = create_embed("CPU Monitor Status", f"Status: **{status}**",
                             0x00ccff if cpu_monitor_active else 0xffaa00)
        embed.add_field(name="Threshold", value=f"{CPU_THRESHOLD}%", inline=True)
        embed.add_field(name="Check Interval", value=f"{CHECK_INTERVAL}s", inline=True)
        await ctx.send(embed=embed)
    elif action.lower() == "enable":
        cpu_monitor_active = True
        await ctx.send(embed=create_success_embed("CPU Monitor Enabled"))
    elif action.lower() == "disable":
        cpu_monitor_active = False
        await ctx.send(embed=create_warning_embed("CPU Monitor Disabled"))
    else:
        await ctx.send(embed=create_error_embed("Invalid Action", "Use: `!cpu-monitor <status|enable|disable>`"))


@bot.command(name='admin-add')
@is_main_admin()
async def admin_add(ctx, user: discord.Member):
    user_id = str(user.id)
    if user_id not in admin_data["admins"]:
        admin_data["admins"].append(user_id)
        save_data()
    await ctx.send(embed=create_success_embed("Admin Added", f"{user.mention} is now an admin."))


@bot.command(name='admin-remove')
@is_main_admin()
async def admin_remove(ctx, user: discord.Member):
    user_id = str(user.id)
    if user_id in admin_data["admins"]:
        admin_data["admins"].remove(user_id)
        save_data()
    await ctx.send(embed=create_success_embed("Admin Removed", f"{user.mention} is no longer an admin."))


@bot.command(name='admin-list')
@is_main_admin()
async def admin_list(ctx):
    admins = []
    for aid in admin_data.get("admins", []):
        try:
            u = await bot.fetch_user(int(aid))
            admins.append(f"• {u.mention} ({u.name})")
        except:
            admins.append(f"• Unknown ({aid})")
    embed = create_info_embed("Admin List", "\n".join(admins) if admins else "No admins")
    await ctx.send(embed=embed)



# ─── Scrollable Help System ────────────────────────────────────────────────────

def build_help_pages(is_user_admin, is_user_main_admin):
    pages = {}

    # Page 2: User Commands
    user_embed = create_embed("👤 User Commands", "VPS management commands for all users:", 0x00ff88)
    user_embed.add_field(name="🖥️ VPS Management", value=(
        "`!manage` — Manage your VPS (Start/Stop/SSH/Reinstall)\n"
        "`!manage [@user]` — Admin: manage another user's VPS\n"
        "`!share-user @user <#>` — Share VPS access\n"
        "`!share-ruser @user <#>` — Revoke shared access\n"
        "`!manage-shared @owner <#>` — Access shared VPS"
    ), inline=False)
    user_embed.add_field(name="🏷️ VPS Tools", value=(
        "`!rename-vps <#> <new_name>` — Give your VPS a nickname\n"
        "`!vps-note <#> <note>` — Add a note to your VPS\n"
        "`!ping-vps <#>` — Ping your VPS container\n"
        "`!uptime-vps <#>` — Check VPS uptime\n"
        "`!myinfo` — View your profile & VPS summary"
    ), inline=False)
    pages["user"] = user_embed

    # Page 3: Credits & Plans
    credits_embed = create_embed("💰 Credits & Plans", "Purchase plans and manage credits:", 0xffaa00)
    credits_embed.add_field(name="💳 Commands", value=(
        "`!plans` — View available VPS plans & prices\n"
        "`!buyc` — Get payment info (UPI/PayPal/Crypto)\n"
        "`!buywc <plan> <Intel/AMD>` — Buy VPS with credits\n"
        "`!credits` — Check your credit balance\n"
        "`!transfer @user <amount>` — Send credits to another user"
    ), inline=False)
    credits_embed.add_field(name="📦 Available Plans", value=(
        "🥉 **Starter** — 4GB RAM | 1 CPU | Intel: 42cr / AMD: 83cr\n"
        "🥈 **Basic** — 8GB RAM | 1 CPU | Intel: 96cr / AMD: 164cr\n"
        "🥇 **Standard** — 12GB RAM | 2 CPU | Intel: 192cr / AMD: 320cr\n"
        "💎 **Pro** — 16GB RAM | 2 CPU | Intel: 220cr / AMD: 340cr"
    ), inline=False)
    pages["credits"] = credits_embed

    # Page 4: VPS Tools
    tools_embed = create_embed("🔧 VPS Tools", "Extra tools to manage & monitor your VPS:", 0x00ccff)
    tools_embed.add_field(name="📊 Monitoring", value=(
        "`!ping-vps <#>` — Ping VPS container (check if alive)\n"
        "`!uptime-vps <#>` — Show how long VPS has been running\n"
        "`!myinfo` — Your full profile: credits, VPS list, notes"
    ), inline=False)
    tools_embed.add_field(name="🏷️ Customization", value=(
        "`!rename-vps <#> <name>` — Set a nickname for your VPS\n"
        "`!vps-note <#> <text>` — Add/update a note on your VPS\n"
        "  e.g. `!vps-note 1 My Minecraft server`"
    ), inline=False)
    tools_embed.add_field(name="💸 Economy", value=(
        "`!transfer @user <amount>` — Send credits to a friend\n"
        "`!leaderboard` — Top 10 credit holders\n"
        "`!botstatus` — Show bot stats & uptime"
    ), inline=False)
    pages["tools"] = tools_embed

    # Page 5: Extras
    extras_embed = create_embed("📢 Extras & Fun", "Announcements, leaderboard, and more:", 0xff6b9d)
    extras_embed.add_field(name="📣 Announcements", value=(
        "`!announce <message>` — Admin: broadcast to all VPS owners via DM\n"
        "`!botstatus` — Bot uptime, total VPS, active users\n"
        "`!leaderboard` — Top credit holders on the server"
    ), inline=False)
    extras_embed.add_field(name="ℹ️ Info", value=(
        "`!help` — Open this help menu\n"
        "`!plans` — VPS plan pricing\n"
        "`!myinfo` — Your personal dashboard"
    ), inline=False)
    pages["extras"] = extras_embed

    # Page 6: Admin Panel
    if is_user_admin:
        admin_embed = create_embed("🛡️ Admin Panel", "Admin-only VPS management commands:", 0xff3366)
        admin_embed.add_field(name="🖥️ VPS Control", value=(
            "`!create @user <ram_GB> <cpu_cores> <disk_GB>` — Create custom Docker VPS\n"
            "`!delete-vps @user <#> <reason>` — Delete a user's VPS\n"
            "`!restart-vps <container>` — Restart a VPS container\n"
            "`!stop-vps-all` — Emergency stop ALL VPS\n"
            "`!exec <container> <cmd>` — Run command inside VPS"
        ), inline=False)
        admin_embed.add_field(name="💾 Backup & Restore", value=(
            "`!backup-vps <container>` — Create Docker image snapshot\n"
            "`!restore-vps <container> <snapshot>` — Restore from snapshot\n"
            "`!list-snapshots <container>` — List all snapshots"
        ), inline=False)
        admin_embed.add_field(name="📊 Info & Economy", value=(
            "`!userinfo @user` — Full user info + VPS list\n"
            "`!serverstats` — Server overview stats\n"
            "`!vpsinfo [container]` — VPS details\n"
            "`!list-all` — All VPS overview\n"
            "`!adminc @user <amount>` — Add credits\n"
            "`!adminrc @user <amount/all>` — Remove credits\n"
            "`!announce <msg>` — DM all VPS owners\n"
            "`!cpu-monitor <status|enable|disable>` — CPU monitor control\n"
            "`!maintenance <on/off>` — Toggle maintenance mode"
        ), inline=False)
        admin_embed.add_field(name="⏳ Expire Management", value=(
            "`!setexpire @user <vps#> <days>` — Set VPS expiry\n"
            "`!extendexpire @user <vps#> <days>` — Extend expiry\n"
            "`!removeexpire @user <vps#>` — Remove expiry (set to Never)\n"
            "`!checkexpire [@user]` — Check expiry status"
        ), inline=False)
        pages["admin"] = admin_embed

    # Page 7: Main Admin
    if is_user_main_admin:
        mainadmin_embed = create_embed("👑 Main Admin", "Exclusive main admin commands:", 0xffd700)
        mainadmin_embed.add_field(name="👥 Admin Management", value=(
            "`!admin-add @user` — Promote user to admin\n"
            "`!admin-remove @user` — Remove admin role\n"
            "`!admin-list` — View all admins"
        ), inline=False)
        pages["mainadmin"] = mainadmin_embed

    return pages


@bot.command(name='help')
async def show_help(ctx):
    """Show scrollable categorized help"""
    user_id = str(ctx.author.id)
    is_user_admin = user_id == str(MAIN_ADMIN_ID) or user_id in admin_data.get("admins", [])
    is_user_main_admin = user_id == str(MAIN_ADMIN_ID)

    pages = build_help_pages(is_user_admin, is_user_main_admin)

    options = [
        discord.SelectOption(label="👤 User Commands", description="VPS manage, share, tools", value="user", emoji="👤"),
        discord.SelectOption(label="💰 Credits & Plans", description="Buy VPS, check plans", value="credits", emoji="💰"),
        discord.SelectOption(label="🔧 VPS Tools", description="Rename, notes, ping, uptime", value="tools", emoji="🔧"),
        discord.SelectOption(label="📢 Extras", description="Announcements, leaderboard", value="extras", emoji="📢"),
    ]
    if is_user_admin:
        options.append(discord.SelectOption(label="🛡️ Admin Panel", description="Admin VPS commands", value="admin", emoji="🛡️"))
    if is_user_main_admin:
        options.append(discord.SelectOption(label="👑 Main Admin", description="Admin management", value="mainadmin", emoji="👑"))

    class HelpSelect(discord.ui.Select):
        def __init__(self):
            super().__init__(placeholder="📂 Select a category...", options=options, min_values=1, max_values=1)

        async def callback(self, interaction: discord.Interaction):
            selected = self.values[0]
            embed = pages.get(selected, pages["user"])
            await interaction.response.edit_message(embed=embed, view=self.view)

    class HelpView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=180)
            self.add_item(HelpSelect())

    await ctx.send(embed=pages["user"], view=HelpView())


# ─── New Cool Features ──────────────────────────────────────────────────────────

BOT_START_TIME = datetime.now()
maintenance_mode = False

@bot.command(name='rename-vps')
async def rename_vps(ctx, vps_number: int, *, new_name: str):
    """Give your VPS a custom nickname"""
    user_id = str(ctx.author.id)
    vps_list = vps_data.get(user_id, [])
    if not vps_list or vps_number < 1 or vps_number > len(vps_list):
        await ctx.send(embed=create_error_embed("Invalid VPS", "VPS not found."))
        return
    if len(new_name) > 30:
        await ctx.send(embed=create_error_embed("Name Too Long", "Nickname must be 30 characters or less."))
        return
    vps_list[vps_number - 1]["nickname"] = new_name
    save_data()
    await ctx.send(embed=create_success_embed("VPS Renamed", f"VPS #{vps_number} is now called **{new_name}**!"))


@bot.command(name='vps-note')
async def vps_note(ctx, vps_number: int, *, note: str):
    """Add a note to your VPS"""
    user_id = str(ctx.author.id)
    vps_list = vps_data.get(user_id, [])
    if not vps_list or vps_number < 1 or vps_number > len(vps_list):
        await ctx.send(embed=create_error_embed("Invalid VPS", "VPS not found."))
        return
    vps_list[vps_number - 1]["note"] = note[:200]
    save_data()
    await ctx.send(embed=create_success_embed("Note Saved", f"Note added to VPS #{vps_number}:\n> {note[:200]}"))


@bot.command(name='ping-vps')
async def ping_vps(ctx, vps_number: int):
    """Ping your VPS container to check if it's alive"""
    user_id = str(ctx.author.id)
    vps_list = vps_data.get(user_id, [])
    if not vps_list or vps_number < 1 or vps_number > len(vps_list):
        await ctx.send(embed=create_error_embed("Invalid VPS", "VPS not found."))
        return
    vps = vps_list[vps_number - 1]
    container = vps["container_name"]
    nickname = vps.get("nickname", f"VPS #{vps_number}")
    msg = await ctx.send(embed=create_info_embed("Pinging...", f"Checking `{container}`..."))
    start = time.time()
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "inspect", "--format={{.State.Running}}", container,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        elapsed = int((time.time() - start) * 1000)
        is_running = stdout.decode().strip() == "true"
        if is_running:
            embed = create_success_embed("🏓 Pong!", f"**{nickname}** is alive!\n⚡ Response: `{elapsed}ms`")
        else:
            embed = create_error_embed("💀 No Response", f"**{nickname}** container is not running.")
        await msg.edit(embed=embed)
    except Exception as e:
        await msg.edit(embed=create_error_embed("Ping Failed", str(e)))


@bot.command(name='uptime-vps')
async def uptime_vps(ctx, vps_number: int):
    """Check VPS uptime"""
    user_id = str(ctx.author.id)
    vps_list = vps_data.get(user_id, [])
    if not vps_list or vps_number < 1 or vps_number > len(vps_list):
        await ctx.send(embed=create_error_embed("Invalid VPS", "VPS not found."))
        return
    vps = vps_list[vps_number - 1]
    container = vps["container_name"]
    nickname = vps.get("nickname", f"VPS #{vps_number}")
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "inspect", "--format={{.State.StartedAt}}", container,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        started_at_str = stdout.decode().strip()
        # Parse docker time format
        started_at = datetime.fromisoformat(started_at_str[:19])
        uptime_delta = datetime.utcnow() - started_at
        days = uptime_delta.days
        hours, rem = divmod(uptime_delta.seconds, 3600)
        minutes, seconds = divmod(rem, 60)
        uptime_str = f"{days}d {hours}h {minutes}m {seconds}s"
        embed = create_success_embed(f"⏱️ Uptime — {nickname}", f"Container has been running for:\n```{uptime_str}```")
        embed.add_field(name="Started At", value=f"`{started_at_str[:19]} UTC`", inline=False)
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(embed=create_error_embed("Uptime Error", str(e)))


@bot.command(name='myinfo')
async def my_info(ctx):
    """Show your personal dashboard"""
    user_id = str(ctx.author.id)
    credits = user_data.get(user_id, {}).get("credits", 0)
    vps_list = vps_data.get(user_id, [])
    embed = create_embed(f"👤 {ctx.author.name}'s Dashboard", "", 0x5865F2)
    embed.set_thumbnail(url=ctx.author.display_avatar.url)
    embed.add_field(name="💰 Credits", value=f"**{credits}**", inline=True)
    embed.add_field(name="🖥️ VPS Count", value=f"**{len(vps_list)}**", inline=True)
    embed.add_field(name="📅 Account", value=f"Joined: {ctx.author.created_at.strftime('%Y-%m-%d')}", inline=True)
    if vps_list:
        vps_text = ""
        for i, v in enumerate(vps_list):
            nickname = v.get("nickname", f"VPS {i+1}")
            status_icon = "🟢" if v.get("status") == "running" else "🔴"
            note = f" — _{v['note']}_" if v.get("note") else ""
            vps_text += f"{status_icon} **{nickname}** (`{v['container_name']}`){note}\n"
        embed.add_field(name="🖥️ Your VPS", value=vps_text, inline=False)
    else:
        embed.add_field(name="🖥️ Your VPS", value="No VPS yet. Use `!buywc` to get one!", inline=False)
    await ctx.send(embed=embed)


@bot.command(name='transfer')
async def transfer_credits(ctx, target: discord.Member, amount: int):
    """Transfer credits to another user"""
    if amount <= 0:
        await ctx.send(embed=create_error_embed("Invalid Amount", "Amount must be positive."))
        return
    if target.id == ctx.author.id:
        await ctx.send(embed=create_error_embed("Invalid Target", "You can't transfer to yourself!"))
        return
    sender_id = str(ctx.author.id)
    target_id = str(target.id)
    if sender_id not in user_data:
        user_data[sender_id] = {"credits": 0}
    if user_data[sender_id]["credits"] < amount:
        await ctx.send(embed=create_error_embed("Insufficient Credits",
            f"You only have **{user_data[sender_id]['credits']}** credits."))
        return
    if target_id not in user_data:
        user_data[target_id] = {"credits": 0}
    user_data[sender_id]["credits"] -= amount
    user_data[target_id]["credits"] += amount
    save_data()
    embed = create_success_embed("💸 Transfer Complete",
        f"{ctx.author.mention} sent **{amount}** credits to {target.mention}!")
    embed.add_field(name="Your Balance", value=f"**{user_data[sender_id]['credits']}** credits", inline=True)
    await ctx.send(embed=embed)
    try:
        await target.send(embed=create_info_embed("💰 Credits Received",
            f"You received **{amount}** credits from {ctx.author.mention}!\nNew balance: **{user_data[target_id]['credits']}**"))
    except discord.Forbidden:
        pass


@bot.command(name='leaderboard')
async def leaderboard(ctx):
    """Show top 10 credit holders"""
    sorted_users = sorted(user_data.items(), key=lambda x: x[1].get("credits", 0), reverse=True)[:10]
    embed = create_embed("🏆 Credit Leaderboard", "Top 10 credit holders:", 0xffd700)
    medals = ["🥇", "🥈", "🥉"] + ["🏅"] * 7
    lines = []
    for i, (uid, data) in enumerate(sorted_users):
        try:
            u = await bot.fetch_user(int(uid))
            name = u.name
        except:
            name = f"User#{uid[:4]}"
        lines.append(f"{medals[i]} **{name}** — {data.get('credits', 0)} credits")
    embed.add_field(name="Rankings", value="\n".join(lines) if lines else "No data yet.", inline=False)
    await ctx.send(embed=embed)


@bot.command(name='botstatus')
async def bot_status(ctx):
    """Show bot status and stats"""
    uptime_delta = datetime.now() - BOT_START_TIME
    days = uptime_delta.days
    hours, rem = divmod(uptime_delta.seconds, 3600)
    minutes, _ = divmod(rem, 60)
    total_vps = sum(len(v) for v in vps_data.values())
    running_vps = sum(1 for vl in vps_data.values() for v in vl if v.get("status") == "running")
    embed = create_embed("🤖 Bot Status", "Slick VPS Manager", 0x00ff88)
    embed.add_field(name="⏱️ Uptime", value=f"`{days}d {hours}h {minutes}m`", inline=True)
    embed.add_field(name="🖥️ Total VPS", value=f"**{total_vps}** ({running_vps} running)", inline=True)
    embed.add_field(name="👥 Users", value=f"**{len(user_data)}**", inline=True)
    embed.add_field(name="🔧 Maintenance", value="🔴 ON" if maintenance_mode else "🟢 OFF", inline=True)
    embed.add_field(name="📡 Latency", value=f"`{round(bot.latency * 1000)}ms`", inline=True)
    await ctx.send(embed=embed)


@bot.command(name='announce')
@is_admin()
async def announce(ctx, *, message: str):
    """Send an announcement DM to all VPS owners (Admin only)"""
    sent = 0
    failed = 0
    announce_embed = create_embed("📢 Announcement", message, 0xffaa00)
    announce_embed.add_field(name="From", value=f"**Slick Team** ({ctx.author.mention})", inline=False)
    status_msg = await ctx.send(embed=create_info_embed("Sending Announcement", "Broadcasting to all VPS owners..."))
    for uid in vps_data.keys():
        try:
            user = await bot.fetch_user(int(uid))
            await user.send(embed=announce_embed)
            sent += 1
            await asyncio.sleep(0.5)  # Rate limit protection
        except:
            failed += 1
    await status_msg.edit(embed=create_success_embed("Announcement Sent",
        f"✅ Delivered to **{sent}** users\n❌ Failed: **{failed}** (DMs closed)"))


@bot.command(name='maintenance')
@is_admin()
async def maintenance_toggle(ctx, mode: str):
    """Toggle maintenance mode (Admin only)"""
    global maintenance_mode
    if mode.lower() == "on":
        maintenance_mode = True
        # Set bot status to Idle + DND-style name
        await bot.change_presence(
            status=discord.Status.idle,
            activity=discord.Activity(type=discord.ActivityType.watching, name="🔴 Under Maintenance")
        )
        await ctx.send(embed=create_warning_embed("🔴 Maintenance Mode ON",
            "Bot is now in maintenance mode.\n"
            "• ALL commands blocked for non-admins\n"
            "• DM commands also blocked\n"
            "• Bot status set to Idle"))
    elif mode.lower() == "off":
        maintenance_mode = False
        # Restore normal status
        await bot.change_presence(
            status=discord.Status.online,
            activity=discord.Activity(type=discord.ActivityType.watching, name="Slick | VPS Manager")
        )
        await ctx.send(embed=create_success_embed("🟢 Maintenance Mode OFF",
            "Bot is back to normal operation."))
    else:
        await ctx.send(embed=create_error_embed("Invalid", "Use: `!maintenance on` or `!maintenance off`"))


# ─── Maintenance mode global check ────────────────────────────────────────────
@bot.check
async def maintenance_check(ctx):
    global maintenance_mode
    if not maintenance_mode:
        return True

    user_id = str(ctx.author.id)
    is_user_admin = user_id == str(MAIN_ADMIN_ID) or user_id in admin_data.get("admins", [])

    # Admins can still use maintenance toggle itself
    if is_user_admin and ctx.command and ctx.command.name == 'maintenance':
        return True

    # Block EVERYONE else — no help, no botstatus, nothing
    if not is_user_admin:
        await ctx.send(embed=create_warning_embed(
            "🔴 Under Maintenance",
            "The bot is currently under maintenance.\n"
            "All commands are disabled until maintenance is complete."
        ))
        return False

    return True


# ─── Block ALL bot commands in DMs — server only ──────────────────────────────
@bot.event
async def on_message(message):
    if message.author.bot:
        return

    # Block ALL commands in DMs for everyone (including admins for normal usage)
    if isinstance(message.channel, discord.DMChannel):
        if message.content.startswith(bot.command_prefix):
            block_embed = create_warning_embed(
                "❌ DM Commands Disabled",
                "Bot commands are **not allowed in DMs**.\n\n"
                "Please use bot commands in the **server channel** only.\n"
                "👉 Go to the server and use commands there!"
            )
            block_embed.set_footer(text="Slick | Server-only bot")
            await message.channel.send(embed=block_embed)
            return  # Never process DM commands

    # If maintenance mode ON — block non-admins in server too
    if maintenance_mode and isinstance(message.channel, discord.TextChannel):
        user_id = str(message.author.id)
        is_user_admin = user_id == str(MAIN_ADMIN_ID) or user_id in admin_data.get("admins", [])
        if not is_user_admin and message.content.startswith(bot.command_prefix):
            await message.channel.send(embed=create_warning_embed(
                "🔴 Under Maintenance",
                "The bot is currently under maintenance.\nAll commands are disabled for users."
            ))
            return

    await bot.process_commands(message)


# Typo aliases
@bot.command(name='mangage')
async def manage_typo(ctx):
    await ctx.send(embed=create_info_embed("Command Correction", "Did you mean `!manage`?"))

@bot.command(name='stats')
async def stats_alias(ctx):
    if str(ctx.author.id) == str(MAIN_ADMIN_ID) or str(ctx.author.id) in admin_data.get("admins", []):
        await server_stats(ctx)
    else:
        await ctx.send(embed=create_error_embed("Access Denied", "Admin only."))


# ─── Expire System ─────────────────────────────────────────────────────────────

@bot.command(name='setexpire')
@is_admin()
async def set_expire(ctx, user: discord.Member, vps_number: int, days: int):
    """Set VPS expiry — !setexpire @user <vps#> <days>"""
    user_id = str(user.id)
    vps_list = vps_data.get(user_id, [])
    if not vps_list or vps_number < 1 or vps_number > len(vps_list):
        await ctx.send(embed=create_error_embed("Not Found", f"{user.mention} has no VPS #{vps_number}."))
        return
    import datetime as dt
    exp_date = (datetime.utcnow() + dt.timedelta(days=days)).isoformat()
    vps_list[vps_number - 1]['expires'] = exp_date
    save_data()
    vps_name = vps_list[vps_number - 1]['container_name']
    await ctx.send(embed=create_success_embed("Expiry Set",
        f"✅ {user.mention}'s **VPS #{vps_number}** (`{vps_name}`) expires on `{exp_date[:10]}` ({days}d from now)."))
    try:
        await user.send(embed=create_warning_embed("⏳ VPS Expiry Set",
            f"Your **VPS #{vps_number}** (`{vps_name}`) has been set to expire on **{exp_date[:10]}** ({days} days).\n"
            f"Contact an admin to extend it before it expires!"))
    except discord.Forbidden:
        pass


@bot.command(name='extendexpire')
@is_admin()
async def extend_expire(ctx, user: discord.Member, vps_number: int, days: int):
    """Extend VPS expiry — !extendexpire @user <vps#> <days>"""
    user_id = str(user.id)
    vps_list = vps_data.get(user_id, [])
    if not vps_list or vps_number < 1 or vps_number > len(vps_list):
        await ctx.send(embed=create_error_embed("Not Found", f"{user.mention} has no VPS #{vps_number}."))
        return
    import datetime as dt
    vps = vps_list[vps_number - 1]
    current = vps.get('expires', 'Never')
    if current == 'Never' or not current:
        base = datetime.utcnow()
    else:
        try:
            base = datetime.fromisoformat(current)
            if base < datetime.utcnow():
                base = datetime.utcnow()
        except:
            base = datetime.utcnow()
    new_exp = (base + dt.timedelta(days=days)).isoformat()
    vps['expires'] = new_exp
    save_data()
    vps_name = vps['container_name']
    await ctx.send(embed=create_success_embed("Expiry Extended",
        f"✅ {user.mention}'s **VPS #{vps_number}** (`{vps_name}`) extended by **{days} days**.\nNew expiry: `{new_exp[:10]}`"))
    try:
        await user.send(embed=create_success_embed("✅ VPS Extended",
            f"Your **VPS #{vps_number}** (`{vps_name}`) has been extended by **{days} days**!\nNew expiry: **{new_exp[:10]}**"))
    except discord.Forbidden:
        pass


@bot.command(name='checkexpire')
async def check_expire(ctx, user: discord.Member = None):
    """Check VPS expiry — users check own, admins can check others"""
    is_user_admin = str(ctx.author.id) == str(MAIN_ADMIN_ID) or str(ctx.author.id) in admin_data.get("admins", [])
    target = user if (user and is_user_admin) else ctx.author
    user_id = str(target.id)
    if user_id not in vps_data or not vps_data[user_id]:
        await ctx.send(embed=create_error_embed("Not Found", f"{target.mention} has no VPS."))
        return
    embed = create_info_embed(f"⏳ VPS Expiry — {target.display_name}", "")
    for i, vps in enumerate(vps_data[user_id]):
        expires = vps.get('expires', 'Never')
        if expires and expires != 'Never':
            try:
                exp_dt = datetime.fromisoformat(expires)
                days_left = (exp_dt - datetime.utcnow()).days
                if days_left < 0:
                    status = f"❌ **EXPIRED** {abs(days_left)}d ago"
                elif days_left <= 3:
                    status = f"⚠️ Expires in **{days_left}d** — {expires[:10]}"
                else:
                    status = f"✅ Expires on `{expires[:10]}` ({days_left}d left)"
            except:
                status = expires
        else:
            status = "♾️ Never (No expiry set)"
        embed.add_field(
            name=f"VPS #{i+1} — `{vps['container_name']}`",
            value=status, inline=False
        )
    await ctx.send(embed=embed)


@bot.command(name='removeexpire')
@is_admin()
async def remove_expire(ctx, user: discord.Member, vps_number: int):
    """Remove expiry from a specific VPS — !removeexpire @user <vps#>"""
    user_id = str(user.id)
    vps_list = vps_data.get(user_id, [])
    if not vps_list or vps_number < 1 or vps_number > len(vps_list):
        await ctx.send(embed=create_error_embed("Not Found", f"{user.mention} has no VPS #{vps_number}."))
        return
    vps_list[vps_number - 1]['expires'] = 'Never'
    save_data()
    vps_name = vps_list[vps_number - 1]['container_name']
    await ctx.send(embed=create_success_embed("Expiry Removed",
        f"✅ {user.mention}'s **VPS #{vps_number}** (`{vps_name}`) expiry set to **Never**."))


# Auto expire checker — runs every hour
@tasks.loop(hours=1)
async def auto_expire_check():
    now = datetime.utcnow()
    to_delete = []
    for user_id, vps_list in list(vps_data.items()):
        for vps in vps_list:
            expires = vps.get('expires', 'Never')
            if not expires or expires == 'Never':
                continue
            try:
                exp_dt = datetime.fromisoformat(expires)
                days_left = (exp_dt - now).days
                # Warn at 3 days
                if days_left == 3:
                    try:
                        u = await bot.fetch_user(int(user_id))
                        await u.send(embed=create_warning_embed(
                            "⚠️ VPS Expiring Soon",
                            f"Your VPS `{vps['container_name']}` expires in **3 days** on `{expires[:10]}`!\n"
                            "Contact an admin to extend it."
                        ))
                    except:
                        pass
                # Warn at 1 day
                elif days_left == 1:
                    try:
                        u = await bot.fetch_user(int(user_id))
                        await u.send(embed=create_error_embed(
                            "🚨 VPS Expiring Tomorrow!",
                            f"Your VPS `{vps['container_name']}` expires **tomorrow** (`{expires[:10]}`)!\n"
                            "Contact an admin IMMEDIATELY to avoid losing access."
                        ))
                    except:
                        pass
                # Expired — stop container
                elif days_left < 0:
                    try:
                        proc = await asyncio.create_subprocess_exec(
                            "docker", "stop", vps['container_name'],
                            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                        )
                        await proc.communicate()
                        vps['status'] = 'stopped'
                    except:
                        pass
                    try:
                        u = await bot.fetch_user(int(user_id))
                        await u.send(embed=create_error_embed(
                            "❌ VPS Expired",
                            f"Your VPS `{vps['container_name']}` has expired and been **stopped**.\n"
                            "Contact an admin to renew it."
                        ))
                    except:
                        pass
            except:
                continue
    save_data()


if __name__ == "__main__":
    token = ""
    bot.run(token)
