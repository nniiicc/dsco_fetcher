import imaplib
import email
from email import policy
import sqlite3
from datetime import datetime
import logging
from bs4 import BeautifulSoup
from dataclasses import dataclass
from typing import List, Optional
import os
from urllib.parse import unquote
import re

@dataclass
class ScholarPublication:
    """Represents a publication from a Google Scholar alert."""
    title: str
    authors: List[str]
    venue: Optional[str]
    year: int
    url: Optional[str]
    scholar_url: Optional[str]
    notification_date: datetime
    email_id: str

class ScholarMonitor:
    def __init__(self, 
                 email_address: str,
                 email_password: str,
                 db_path: str = "scholar_publications.db",
                 imap_server: str = "imap.gmail.com"):
        """Initialize the Scholar monitor."""
        self.email_address = email_address
        self.email_password = email_password
        self.imap_server = imap_server
        self.db_path = db_path
        self.logger = self._setup_logging()
        self.setup_database()

    def _setup_logging(self) -> logging.Logger:
        """Setup logging configuration."""
        logger = logging.getLogger('ScholarMonitor')
        logger.setLevel(logging.INFO)
        
        # File handler
        fh = logging.FileHandler('scholar_monitor.log')
        fh.setLevel(logging.INFO)
        
        # Console handler
        ch = logging.StreamHandler()
        ch.setLevel(logging.INFO)
        
        # Formatter
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        fh.setFormatter(formatter)
        ch.setFormatter(formatter)
        
        logger.addHandler(fh)
        logger.addHandler(ch)
        
        return logger

    def setup_database(self):
        """Setup SQLite database for storing publications."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS publications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                authors TEXT NOT NULL,
                venue TEXT,
                year INTEGER,
                url TEXT,
                scholar_url TEXT,
                notification_date TEXT NOT NULL,
                email_id TEXT UNIQUE,
                UNIQUE(title, authors, year)
            )
        """)
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS processed_emails (
                email_id TEXT PRIMARY KEY,
                process_date TEXT NOT NULL
            )
        """)
        
        conn.commit()
        conn.close()

    def connect_to_email(self) -> imaplib.IMAP4_SSL:
        """Connect to email server and select inbox."""
        try:
            mail = imaplib.IMAP4_SSL(self.imap_server)
            mail.login(self.email_address, self.email_password)
            mail.select("INBOX")
            return mail
        except Exception as e:
            self.logger.error(f"Error connecting to email: {e}")
            raise

    def parse_scholar_email(self, email_message: email.message.EmailMessage) -> Optional[ScholarPublication]:
        """Parse Google Scholar notification email into a Publication object."""
        try:
            # Get HTML content
            html_content = None
            if email_message.is_multipart():
                for part in email_message.walk():
                    if part.get_content_type() == "text/html":
                        html_content = part.get_payload(decode=True).decode()
                        break
            else:
                if email_message.get_content_type() == "text/html":
                    html_content = email_message.get_payload(decode=True).decode()

            if not html_content:
                self.logger.warning("No HTML content found in email")
                return None

            # Parse HTML
            soup = BeautifulSoup(html_content, 'html.parser')
            
            # Find the main article link (title)
            title_link = soup.find('a', class_='gse_alrt_title')
            if not title_link:
                self.logger.warning("No title found in email")
                return None
                
            title = title_link.get_text(strip=True)
            article_url = None
            if 'href' in title_link.attrs:
                url_match = re.search(r'url=([^&]+)', title_link['href'])
                if url_match:
                    article_url = unquote(url_match.group(1))
            
            # Find author and venue info (in div with specific color)
            author_venue_div = soup.find('div', style=lambda x: x and '#006621' in x)
            authors = []
            venue = None
            year = datetime.now().year
            
            if author_venue_div:
                # Text format: "Author1, Author2 - Venue, Year"
                author_venue_text = author_venue_div.get_text(strip=True)
                if ' - ' in author_venue_text:
                    authors_part, venue_part = author_venue_text.split(' - ', 1)
                    authors = [author.strip() for author in authors_part.split(',')]
                    
                    # Extract venue and year
                    venue_match = re.match(r'(.*?),\s*(\d{4})', venue_part)
                    if venue_match:
                        venue = venue_match.group(1).strip()
                        year = int(venue_match.group(2))
            
            # Find Scholar URL
            scholar_url = None
            scholar_links = soup.find_all('a', href=lambda x: x and 'scholar.google.com/citations' in x)
            if scholar_links:
                scholar_url = unquote(scholar_links[0]['href'])

            return ScholarPublication(
                title=title,
                authors=authors,
                venue=venue,
                year=year,
                url=article_url,
                scholar_url=scholar_url,
                notification_date=datetime.now(),
                email_id=email_message['Message-ID']
            )

        except Exception as e:
            self.logger.error(f"Error parsing email: {e}")
            return None

    def store_publication(self, pub: ScholarPublication):
        """Store publication in database."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            cursor.execute("""
                INSERT INTO publications 
                (title, authors, venue, year, url, scholar_url, 
                 notification_date, email_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                pub.title,
                ','.join(pub.authors),
                pub.venue,
                pub.year,
                pub.url,
                pub.scholar_url,
                pub.notification_date.isoformat(),
                pub.email_id
            ))
            
            cursor.execute("""
                INSERT INTO processed_emails (email_id, process_date)
                VALUES (?, ?)
            """, (pub.email_id, datetime.now().isoformat()))
            
            conn.commit()
            self.logger.info(f"Stored publication: {pub.title}")
            
        except sqlite3.IntegrityError:
            self.logger.info(f"Publication already exists: {pub.title}")
        except Exception as e:
            self.logger.error(f"Error storing publication: {e}")
            conn.rollback()
        finally:
            conn.close()

    def process_new_alerts(self):
        """Process new Google Scholar alert emails."""
        mail = self.connect_to_email()
        
        try:
            # Search for unread emails from Google Scholar
            _, message_numbers = mail.search(None, '(UNSEEN FROM "scholaralerts-noreply@google.com")')
            
            for num in message_numbers[0].split():
                try:
                    # Fetch email message
                    _, msg_data = mail.fetch(num, '(RFC822)')
                    email_body = msg_data[0][1]
                    email_message = email.message_from_bytes(email_body, policy=policy.default)
                    
                    # Parse publication data
                    publication = self.parse_scholar_email(email_message)
                    
                    if publication:
                        # Store the publication
                        self.store_publication(publication)
                        
                        # Mark email as read
                        mail.store(num, '+FLAGS', '\\Seen')
                        
                        self.logger.info(f"Processed alert for: {publication.title}")
                    
                except Exception as e:
                    self.logger.error(f"Error processing email {num}: {e}")
                    continue
                
        finally:
            mail.logout()

def monitor_scholar_alerts(email_address: str, 
                         email_password: str, 
                         check_interval: int = 3600):
    """
    Continuously monitor for new Scholar alerts.
    
    Args:
        email_address: Gmail address
        email_password: Gmail app password
        check_interval: Time between checks in seconds (default: 1 hour)
    """
    monitor = ScholarMonitor(email_address, email_password)
    
    while True:
        try:
            monitor.process_new_alerts()
            monitor.logger.info(f"Sleeping for {check_interval} seconds...")
            time.sleep(check_interval)
        except Exception as e:
            monitor.logger.error(f"Error in monitoring loop: {e}")
            time.sleep(300)  # Wait 5 minutes before retrying

if __name__ == "__main__":
    import os
    from dotenv import load_dotenv
    import time
    
    # Load email credentials from environment variables
    load_dotenv()
    
    email_address = os.getenv("SCHOLAR_EMAIL")
    email_password = os.getenv("SCHOLAR_PASSWORD")
    
    if not email_address or not email_password:
        print("Please set SCHOLAR_EMAIL and SCHOLAR_PASSWORD environment variables")
        exit(1)
    
    # Start monitoring
    monitor_scholar_alerts(email_address, email_password)
