"""Local administrator CLI; role assignment is never exposed to HTTP clients."""
import argparse


def main():
    parser = argparse.ArgumentParser(description='Assign registration roles and course restrictions.')
    commands = parser.add_subparsers(dest='command', required=True)
    profile = commands.add_parser('profile')
    profile.add_argument('email')
    profile.add_argument('--role', choices=['student', 'advisor'], required=True)
    profile.add_argument('--department', required=True)
    policy = commands.add_parser('course')
    policy.add_argument('code')
    policy.add_argument('--department', required=True)
    policy.add_argument('--restricted', choices=['yes', 'no'], required=True)
    args = parser.parse_args()
    department = args.department.strip().upper()
    if not department or len(department) > 40:
        parser.error('Department must contain 1–40 characters.')
    import main as application
    service = application.app.state.registration
    with application.app.state.waitlists.transaction() as db:
        if args.command == 'profile':
            user = db.query(application.UserModel).filter_by(email=args.email.strip().lower()).first()
            if not user:
                parser.error('Register this user through the application first.')
            existing = db.get(service.Profile, user.id)
            if existing and existing.department != department:
                pending = db.query(service.Override).filter_by(user_id=user.id, status='pending').first()
                if pending:
                    parser.error('Resolve pending requests before changing departments.')
            db.merge(service.Profile(user_id=user.id, role=args.role, department=department))
        else:
            code = args.code.strip().upper()
            if code not in service.credits:
                parser.error('Course is not in the official curriculum.')
            db.merge(service.Policy(course=code, department=department, restricted=int(args.restricted == 'yes')))
    print('Registration configuration saved.')


if __name__ == '__main__':
    main()
