import nextcord
from nextcord.ext import commands
from gamercon_async import EvrimaRCON
import paramiko
import asyncio
import logging
import aiosqlite
from datetime import datetime
import posixpath
import stat
from util.config import FTP_HOST, FTP_PASS, FTP_PORT, FTP_USER, ENABLE_LOGGING, LINK_CHANNEL, RCON_HOST, RCON_PORT, RCON_PASS
from util.database import DB_PATH
import util.database as db_utils

# Systerm still being developed, don't complain.
class DinoStorage(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.ftp_host = FTP_HOST
        self.ftp_port = FTP_PORT
        self.ftp_username = FTP_USER
        self.ftp_password = FTP_PASS
        self.rcon_host = RCON_HOST
        self.rcon_port = RCON_PORT
        self.rcon_password = RCON_PASS

    async def run_rcon(self, command):
        try:
            rcon = EvrimaRCON(self.rcon_host, self.rcon_port, self.rcon_password)
            await rcon.connect()
            return await rcon.send_command(command)
        except Exception as e:
            logging.error(f"RCON operation error: {e}")
            return None

    async def run_rcon_admin_command(self, admin_command: str):
        # 0x16 is the admin text-command opcode (e.g. kill command string).
        command = b'\x02' + b'\x16' + admin_command.encode() + b'\x00'
        return await self.run_rcon(command)

    async def is_player_in_game(self, steam_id: str):
        command = b'\x02' + b'\x40' + b'\x00'
        response = await self.run_rcon(command)
        if response is None:
            return False
        return steam_id in str(response)

    async def kill_dino_with_rcon(self, steam_id: str):
        # Some setups expect "kill <id>", while others require "/kill <id>".
        # Try both and accept the first successful response.
        response = await self.run_rcon_admin_command(f"kill {steam_id}")
        if response is not None:
            return True

        response = await self.run_rcon_admin_command(f"/kill {steam_id}")
        return response is not None

    async def async_sftp_operation(self, operation, *args, **kwargs):
        loop = asyncio.get_event_loop()
        try:
            with paramiko.Transport((self.ftp_host, self.ftp_port)) as transport:
                transport.connect(username=self.ftp_username, password=self.ftp_password)
                sftp = paramiko.SFTPClient.from_transport(transport)
                try:
                    result = await loop.run_in_executor(None, operation, sftp, *args, **kwargs)
                    return result
                finally:
                    sftp.close()
        except Exception as e:
            logging.error(f"SFTP operation error: {e}")
            return None

    def ensure_remote_directory(self, sftp, directory):
        if not directory:
            return

        current = ""
        for part in directory.strip("/").split("/"):
            current = f"{current}/{part}" if current else f"/{part}"
            try:
                sftp.stat(current)
            except IOError:
                sftp.mkdir(current)

    def copy_remote_file(self, sftp, src, dest):
        try:
            self.ensure_remote_directory(sftp, posixpath.dirname(dest))
            with sftp.open(src, "rb") as fsrc:
                data = fsrc.read()
            with sftp.open(dest, "wb") as fdest:
                fdest.write(data)
            return True
        except Exception as e:
            logging.error(f"Error copying remote file: {e}")
            return False

    def delete_remote_file(self, sftp, path):
        try:
            sftp.remove(path)
            return True
        except Exception as e:
            try:
                sftp.stat(path)
                logging.error(f"Error deleting remote file: {e}")
                return False
            except Exception:
                return True

    def remote_file_exists(self, sftp, path):
        try:
            sftp.stat(path)
            return True
        except Exception:
            return False

    def list_remote_files(self, sftp, path):
        try:
            return [name for name in sftp.listdir(path) if name.endswith(".sav")]
        except Exception:
            return []

    def list_remote_entries(self, sftp, path):
        try:
            entries = []
            for attr in sftp.listdir_attr(path):
                entries.append(
                    {
                        "name": attr.filename,
                        "is_dir": stat.S_ISDIR(attr.st_mode),
                    }
                )
            return entries
        except Exception:
            return []

    def list_playerdata_files_for_steam(self, sftp, steam_id: str):
        base_path = "/TheIsle/Saved/PlayerData"
        try:
            names = sftp.listdir(base_path)
        except Exception:
            return []

        matched = []
        for name in names:
            # Keep every file that belongs to this steam id (sav + companion files).
            if name == steam_id or name.startswith(f"{steam_id}."):
                full_path = f"{base_path}/{name}"
                try:
                    file_attr = sftp.stat(full_path)
                    if not stat.S_ISDIR(file_attr.st_mode):
                        matched.append(name)
                except Exception:
                    continue
        return matched

    def list_files_in_directory(self, sftp, path):
        try:
            files = []
            for attr in sftp.listdir_attr(path):
                if not stat.S_ISDIR(attr.st_mode):
                    files.append(attr.filename)
            return files
        except Exception:
            return []

    def remove_remote_directory(self, sftp, path):
        try:
            for name in sftp.listdir(path):
                child_path = f"{path}/{name}"
                try:
                    child_attr = sftp.stat(child_path)
                    if stat.S_ISDIR(child_attr.st_mode):
                        self.remove_remote_directory(sftp, child_path)
                    else:
                        sftp.remove(child_path)
                except Exception:
                    continue
            sftp.rmdir(path)
            return True
        except Exception as e:
            logging.error(f"Error removing remote directory: {e}")
            return False

    async def get_linked_steam_id(self, discord_id: str):
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT steam_id FROM links WHERE discord_id = ? AND status = ? ORDER BY id DESC LIMIT 1",
                (discord_id, "linked")
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    return row[0]
                return None

    async def defer_interaction(self, interaction: nextcord.Interaction):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)

    async def send_interaction_message(
        self,
        interaction: nextcord.Interaction,
        *,
        content: str = None,
        embed: nextcord.Embed = None,
        view: nextcord.ui.View = None,
        ephemeral: bool = True,
    ):
        try:
            if interaction.response.is_done():
                await interaction.followup.send(content=content, embed=embed, view=view, ephemeral=ephemeral)
            else:
                await interaction.response.send_message(content=content, embed=embed, view=view, ephemeral=ephemeral)
        except nextcord.errors.NotFound:
            logging.warning("Interaction expired before response could be sent.")

    async def save_current_dino(self, interaction: nextcord.Interaction):
        await self.defer_interaction(interaction)
        discord_id = str(interaction.user.id)
        steam_id = await self.get_linked_steam_id(discord_id)
        if not steam_id:
            embed = nextcord.Embed(
                title="Dino Garage",
                description="Du bist nicht verbunden. Nutze den Button **Verbinden**.",
                color=0xFF0000,
            )
            await self.send_interaction_message(interaction, embed=embed, ephemeral=True)
            return

        src_sav = f"/TheIsle/Saved/PlayerData/{steam_id}.sav"
        slot_timestamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        slot_name = slot_timestamp
        slot_dir = f"/TheIsle/Saved/Garage/{discord_id}/{slot_name}"

        in_game = await self.is_player_in_game(steam_id)
        if not in_game:
            embed = nextcord.Embed(
                title="Dino Garage",
                description="Du musst im Spiel sein, um deinen Dino zu speichern.",
                color=0xFF0000,
            )
            await self.send_interaction_message(interaction, embed=embed, ephemeral=True)
            return

        source_exists = await self.async_sftp_operation(self.remote_file_exists, src_sav)
        if not source_exists:
            embed = nextcord.Embed(
                title="Dino Garage",
                description="Kein aktiver Dino gefunden. In deiner PlayerData liegt aktuell kein Save.",
                color=0xFF0000,
            )
            await self.send_interaction_message(interaction, embed=embed, ephemeral=True)
            return

        player_files = await self.async_sftp_operation(self.list_playerdata_files_for_steam, steam_id)
        if not player_files:
            embed = nextcord.Embed(
                title="Dino Garage",
                description="Speichern fehlgeschlagen.",
                color=0xFF0000,
            )
            await self.send_interaction_message(interaction, embed=embed, ephemeral=True)
            return

        copied_files = []
        for filename in player_files:
            src_file = f"/TheIsle/Saved/PlayerData/{filename}"
            dest_file = f"{slot_dir}/{filename}"
            copy_result = await self.async_sftp_operation(self.copy_remote_file, src_file, dest_file)
            if not copy_result:
                embed = nextcord.Embed(
                    title="Dino Garage",
                    description="Speichern fehlgeschlagen. Nicht alle Dino-Dateien konnten gesichert werden.",
                    color=0xFF0000,
                )
                await self.send_interaction_message(interaction, embed=embed, ephemeral=True)
                return
            copied_files.append(filename)

        kill_result = await self.kill_dino_with_rcon(steam_id)
        if not kill_result:
            embed = nextcord.Embed(
                title="Dino Garage",
                description="Speichern abgeschlossen, aber /kill konnte nicht ausgefuehrt werden.",
                color=0xFFA500,
            )
            await self.send_interaction_message(interaction, embed=embed, ephemeral=True)
            return

        delete_failed = False
        for filename in copied_files:
            src_file = f"/TheIsle/Saved/PlayerData/{filename}"
            delete_result = await self.async_sftp_operation(self.delete_remote_file, src_file)
            if delete_result is False:
                delete_failed = True

        if delete_failed:
            embed = nextcord.Embed(
                title="Dino Garage",
                description=(
                    "Dino wurde gespeichert, aber der Original-Dino konnte nicht entfernt werden. "
                    "Bitte Server-Logs pruefen."
                ),
                color=0xFFA500,
            )
            await self.send_interaction_message(interaction, embed=embed, ephemeral=True)
            return

        if LINK_CHANNEL:
            channel = self.bot.get_channel(int(LINK_CHANNEL))
            if channel:
                channel_embed = nextcord.Embed(
                    title="Dino Storage",
                    description=f"User {interaction.user.id} hat Dino-Slot {slot_name} gespeichert.",
                    color=0x00FF00,
                )
                await channel.send(embed=channel_embed)

        embed = nextcord.Embed(
            title="Dino Garage",
            description=(
                f"Dino gespeichert als Slot **{slot_name}**. "
                f"{len(copied_files)} Datei(en) wurden gesichert (inkl. Growth-Daten). "
                "Original wurde mit /kill entfernt."
            ),
            color=0x00FF00,
        )
        await self.send_interaction_message(interaction, embed=embed, ephemeral=True)

    async def list_user_slots(self, discord_id: str):
        base_path = f"/TheIsle/Saved/Garage/{discord_id}"
        entries = await self.async_sftp_operation(self.list_remote_entries, base_path)
        if entries is None:
            return []

        slots = []

        for entry in entries:
            full_path = f"{base_path}/{entry['name']}"
            if entry["is_dir"]:
                slots.append({"name": entry["name"], "path": full_path, "is_legacy": False})
            elif entry["name"].endswith(".sav"):
                # Backward compatibility with old single-file slots.
                slots.append({"name": entry["name"], "path": full_path, "is_legacy": True})

        slots = sorted(slots, key=lambda slot: slot["name"], reverse=True)
        return slots

    async def show_saved_dinos(self, interaction: nextcord.Interaction):
        await self.defer_interaction(interaction)
        slots = await self.list_user_slots(str(interaction.user.id))
        if not slots:
            embed = nextcord.Embed(
                title="Meine Dinos",
                description="Du hast aktuell keine gespeicherten Dinos.",
                color=0xFF0000,
            )
            await self.send_interaction_message(interaction, embed=embed, ephemeral=True)
            return

        view = DinoSlotPickerView(self, interaction.user.id, slots)
        embed = nextcord.Embed(
            title="Meine Dinos",
            description="Waehle einen Dino-Slot aus.",
            color=0x5865F2,
        )
        await self.send_interaction_message(interaction, embed=embed, view=view, ephemeral=True)

    async def load_slot_to_playerdata(self, interaction: nextcord.Interaction, slot_path: str, slot_name: str):
        await self.defer_interaction(interaction)
        discord_id = str(interaction.user.id)
        steam_id = await self.get_linked_steam_id(discord_id)
        if not steam_id:
            embed = nextcord.Embed(
                title="Dino Garage",
                description="Du bist nicht verbunden. Nutze den Button **Verbinden**.",
                color=0xFF0000,
            )
            await self.send_interaction_message(interaction, embed=embed, ephemeral=True)
            return

        in_game = await self.is_player_in_game(steam_id)
        if not in_game:
            embed = nextcord.Embed(
                title="Dino Garage",
                description="Du musst im Spiel sein und deinen Original-Dino ausgewaehlt haben, bevor du laedst.",
                color=0xFF0000,
            )
            await self.send_interaction_message(interaction, embed=embed, ephemeral=True)
            return

        source_exists = await self.async_sftp_operation(self.remote_file_exists, slot_path)
        if not source_exists:
            embed = nextcord.Embed(
                title="Dino Garage",
                description="Der ausgewaehlte Slot existiert nicht mehr.",
                color=0xFF0000,
            )
            await self.send_interaction_message(interaction, embed=embed, ephemeral=True)
            return

        playerdata_path = f"/TheIsle/Saved/PlayerData/{steam_id}.sav"
        original_exists = await self.async_sftp_operation(self.remote_file_exists, playerdata_path)
        if not original_exists:
            embed = nextcord.Embed(
                title="Dino Garage",
                description="Kein Original-Dino gefunden. Waehle zuerst deinen aktuellen Dino im Spiel aus.",
                color=0xFF0000,
            )
            await self.send_interaction_message(interaction, embed=embed, ephemeral=True)
            return

        current_files = await self.async_sftp_operation(self.list_playerdata_files_for_steam, steam_id)
        if not current_files:
            embed = nextcord.Embed(
                title="Dino Garage",
                description="Aktuelle PlayerData konnte nicht gelesen werden.",
                color=0xFF0000,
            )
            await self.send_interaction_message(interaction, embed=embed, ephemeral=True)
            return

        for filename in current_files:
            deletion_result = await self.async_sftp_operation(self.delete_remote_file, f"/TheIsle/Saved/PlayerData/{filename}")
            if deletion_result is False:
                embed = nextcord.Embed(
                    title="Dino Garage",
                    description="Aktuelle PlayerData konnte nicht entfernt werden.",
                    color=0xFF0000,
                )
                await self.send_interaction_message(interaction, embed=embed, ephemeral=True)
                return

        slot_attr_is_dir = await self.async_sftp_operation(
            lambda sftp, path: stat.S_ISDIR(sftp.stat(path).st_mode),
            slot_path,
        )

        restored_count = 0
        if slot_attr_is_dir:
            slot_files = await self.async_sftp_operation(self.list_files_in_directory, slot_path)
            if not slot_files:
                embed = nextcord.Embed(
                    title="Dino Garage",
                    description="Slot ist leer und kann nicht geladen werden.",
                    color=0xFF0000,
                )
                await self.send_interaction_message(interaction, embed=embed, ephemeral=True)
                return

            for filename in slot_files:
                src_file = f"{slot_path}/{filename}"
                dest_file = f"/TheIsle/Saved/PlayerData/{filename}"
                copy_result = await self.async_sftp_operation(self.copy_remote_file, src_file, dest_file)
                if not copy_result:
                    embed = nextcord.Embed(
                        title="Dino Garage",
                        description="Laden fehlgeschlagen. Slot-Dateien konnten nicht wiederhergestellt werden.",
                        color=0xFF0000,
                    )
                    await self.send_interaction_message(interaction, embed=embed, ephemeral=True)
                    return
                restored_count += 1
        else:
            copy_result = await self.async_sftp_operation(self.copy_remote_file, slot_path, playerdata_path)
            if not copy_result:
                embed = nextcord.Embed(
                    title="Dino Garage",
                    description="Laden fehlgeschlagen.",
                    color=0xFF0000,
                )
                await self.send_interaction_message(interaction, embed=embed, ephemeral=True)
                return
            restored_count = 1

        kill_result = await self.kill_dino_with_rcon(steam_id)
        if not kill_result:
            embed = nextcord.Embed(
                title="Dino Garage",
                description="Slot geladen, aber /kill konnte nicht ausgefuehrt werden. Bitte reloggen.",
                color=0xFFA500,
            )
            await self.send_interaction_message(interaction, embed=embed, ephemeral=True)
            return

        embed = nextcord.Embed(
            title="Dino Garage",
            description=(
                f"Slot **{slot_name}** wurde vollstaendig geladen. "
                f"{restored_count} Datei(en) wurden wiederhergestellt. "
                "Alle gespeicherten Dino-Daten wurden auf deinen aktuellen Dino angewendet."
            ),
            color=0x00FF00,
        )
        await self.send_interaction_message(interaction, embed=embed, ephemeral=True)

    async def delete_slot(self, interaction: nextcord.Interaction, slot_path: str, slot_name: str):
        await self.defer_interaction(interaction)
        slot_attr_is_dir = await self.async_sftp_operation(
            lambda sftp, path: stat.S_ISDIR(sftp.stat(path).st_mode),
            slot_path,
        )
        if slot_attr_is_dir is None:
            embed = nextcord.Embed(
                title="Dino Garage",
                description="Slot konnte nicht geprueft werden.",
                color=0xFF0000,
            )
            await self.send_interaction_message(interaction, embed=embed, ephemeral=True)
            return

        if slot_attr_is_dir:
            deletion_result = await self.async_sftp_operation(self.remove_remote_directory, slot_path)
        else:
            deletion_result = await self.async_sftp_operation(self.delete_remote_file, slot_path)

        if deletion_result is False:
            embed = nextcord.Embed(
                title="Dino Garage",
                description="Slot konnte nicht geloescht werden.",
                color=0xFF0000,
            )
            await self.send_interaction_message(interaction, embed=embed, ephemeral=True)
            return

        embed = nextcord.Embed(
            title="Dino Garage",
            description=f"Slot **{slot_name}** wurde geloescht.",
            color=0x00FF00,
        )
        await self.send_interaction_message(interaction, embed=embed, ephemeral=True)

    @nextcord.slash_command(description="Open the German dino panel.")
    async def dinopanel(self, interaction: nextcord.Interaction):
        embed = nextcord.Embed(
            title="Dino Panel",
            description="Nutze die Buttons unten: **Verbinden**, **Speichern**, **Meine Dinos**",
            color=0x2ECC71,
        )
        await interaction.response.send_message(embed=embed, view=DinoPanelView(self), ephemeral=True)

    @nextcord.slash_command(description="Store your dino.")
    async def store(self, interaction: nextcord.Interaction):
        await self.save_current_dino(interaction)

    @nextcord.slash_command(description="Load your stored dino.")
    async def load(self, interaction: nextcord.Interaction):
        slots = await self.list_user_slots(str(interaction.user.id))
        if not slots:
            embed = nextcord.Embed(
                title="Dino Garage",
                description="Du hast keine gespeicherten Slots.",
                color=0xFF0000,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        latest_slot = slots[0]
        await self.load_slot_to_playerdata(interaction, latest_slot["path"], latest_slot["name"])


class SteamLinkModal(nextcord.ui.Modal):
    def __init__(self):
        super().__init__("Verbinden")
        self.steam_id = nextcord.ui.TextInput(
            label="Steam ID",
            placeholder="Beispiel: 7656119xxxxxxxxxx",
            min_length=5,
            max_length=32,
            required=True,
        )
        self.add_item(self.steam_id)

    async def callback(self, interaction: nextcord.Interaction):
        steam_id = str(self.steam_id.value).strip()
        if not steam_id.isdigit():
            await interaction.response.send_message(
                "Steam ID muss nur aus Zahlen bestehen.",
                ephemeral=True,
            )
            return

        await db_utils.set_linked_steam(str(interaction.user.id), steam_id)
        await interaction.response.send_message(
            f"Dein Account wurde mit Steam ID **{steam_id}** verbunden.",
            ephemeral=True,
        )


class DinoPanelView(nextcord.ui.View):
    def __init__(self, cog: DinoStorage):
        super().__init__(timeout=None)
        self.cog = cog

    @nextcord.ui.button(label="Verbinden", style=nextcord.ButtonStyle.primary)
    async def connect_button(self, _button: nextcord.ui.Button, interaction: nextcord.Interaction):
        await interaction.response.send_modal(SteamLinkModal())

    @nextcord.ui.button(label="Speichern", style=nextcord.ButtonStyle.success)
    async def store_button(self, _button: nextcord.ui.Button, interaction: nextcord.Interaction):
        await self.cog.save_current_dino(interaction)

    @nextcord.ui.button(label="Meine Dinos", style=nextcord.ButtonStyle.secondary)
    async def my_dinos_button(self, _button: nextcord.ui.Button, interaction: nextcord.Interaction):
        await self.cog.show_saved_dinos(interaction)


class DinoSlotSelect(nextcord.ui.Select):
    def __init__(self, cog: DinoStorage, owner_id: int, slots):
        options = [
            nextcord.SelectOption(label=slot["name"], value=slot["path"], description="Gespeicherter Dino-Slot")
            for slot in slots[:25]
        ]
        super().__init__(placeholder="Wahle deinen gespeicherten Dino", min_values=1, max_values=1, options=options)
        self.cog = cog
        self.owner_id = owner_id
        self.slots = slots

    async def callback(self, interaction: nextcord.Interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Du kannst diese Auswahl nicht benutzen.", ephemeral=True)
            return

        selected_path = self.values[0]
        selected_slot = next((slot for slot in self.slots if slot["path"] == selected_path), None)
        if not selected_slot:
            await interaction.response.send_message("Ausgewaehlter Slot wurde nicht gefunden.", ephemeral=True)
            return

        view = DinoSlotActionView(self.cog, self.owner_id, selected_slot["path"], selected_slot["name"])
        embed = nextcord.Embed(
            title="Dino Aktion",
            description=f"Slot **{selected_slot['name']}** ausgwaehlt. Was moechtest du tun?",
            color=0x5865F2,
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class DinoSlotPickerView(nextcord.ui.View):
    def __init__(self, cog: DinoStorage, owner_id: int, slots):
        super().__init__(timeout=180)
        self.add_item(DinoSlotSelect(cog, owner_id, slots))


class DinoSlotActionView(nextcord.ui.View):
    def __init__(self, cog: DinoStorage, owner_id: int, slot_path: str, slot_name: str):
        super().__init__(timeout=180)
        self.cog = cog
        self.owner_id = owner_id
        self.slot_path = slot_path
        self.slot_name = slot_name

    async def interaction_check(self, interaction: nextcord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Nur du kannst diese Aktion nutzen.", ephemeral=True)
            return False
        return True

    @nextcord.ui.button(label="Laden", style=nextcord.ButtonStyle.success)
    async def load_button(self, _button: nextcord.ui.Button, interaction: nextcord.Interaction):
        await self.cog.load_slot_to_playerdata(interaction, self.slot_path, self.slot_name)

    @nextcord.ui.button(label="Loeschen", style=nextcord.ButtonStyle.danger)
    async def delete_button(self, _button: nextcord.ui.Button, interaction: nextcord.Interaction):
        await self.cog.delete_slot(interaction, self.slot_path, self.slot_name)

def setup(bot):
    if not ENABLE_LOGGING:
        return
    cog = DinoStorage(bot)
    bot.add_cog(cog)
    if not hasattr(bot, "all_slash_commands"):
        bot.all_slash_commands = []
    bot.all_slash_commands.extend(
        [
            cog.dinopanel,
            cog.store,
            cog.load,
        ]
    )
